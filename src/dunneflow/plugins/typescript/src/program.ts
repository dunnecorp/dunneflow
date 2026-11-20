/**
 * The compilation, built in `open` — which is what `open` was always for.
 *
 * Chunk 9 held the file set and built nothing, and said so: a `ts.createProgram` over
 * `node_modules` paid for nothing would have been the easy thing to leave in. This is the
 * chunk that spends it, because **the resolved-symbol information the plugin exists to
 * provide only exists inside a program** — a per-file parse has no checker and is reduced
 * to guessing by name, which is the thing using the native toolchain is meant to stop.
 *
 * **One program per `tsconfig.json`, not one for the tree.** `PLAN-v2.md` puts the growth
 * axis at one worker per project; this is the same axis realised inside the plugin, which
 * costs the host nothing — no protocol change, `workers` still 1, the same file set. It is
 * not tidiness: a monorepo's projects disagree about compiler options in ways that decide
 * what resolves. `../DunneNote`'s `legacy/v0.1` declares `paths: { "$lib/*": ["src/lib/*"] }`
 * relative to itself, and one options object holds one `paths` map — so a single program
 * would silently fail to resolve every `$lib` import in one of the two copies of that
 * program, and report the calls as external with nothing anywhere saying why (rule 49).
 *
 * **Root names are the host's file set, never the tsconfig's `include`.** The host has
 * already decided what is program code and what is a test — 315 of DunneNote's 758 `.ts`
 * files are tests — and a program built from `include` would put every one of them back,
 * then resolve calls into them. Whatever a root file *imports* is pulled in by the
 * compiler and is a legitimate far end; whatever it does not is not in the map, and a call
 * to it is external.
 *
 * **Files under no tsconfig are a group of their own, not an error.** `packages/api-types`
 * has a `package.json` and no `tsconfig.json`, and its 206 files are real program code.
 * They get the default options, which is what `tsc` on a bare file does.
 */

import ts from "typescript";
import { dirname, join, relative, resolve, sep } from "node:path";
import { existsSync, readFileSync } from "node:fs";
import { makeSourceFile } from "./extract.js";
import { LIB_DIR, TS_LIBS } from "./libs.js";

/** `ingest.contract.SOURCE_TEXT`, applied at the one place this plugin reads a file. */
export function readSource(abs: string): string {
  return readFileSync(abs, "utf8").replace(/\r\n?/g, "\n");
}

/**
 * The cache's one spelling of a path, and it is the compiler's: forward slashes on every
 * platform, which is how the host is asked for a file (`C:/…/a.ts`) while `main.ts`
 * resolves the same file to `C:\…\a.ts`. Keyed as each side spelt it, the lookup in
 * `sourceFile` missed on Windows and handed the extractor a second, **unbound** parse of
 * the file, and the checker crashed on the first node it was asked about ("Cannot read
 * properties of undefined (reading 'members')"). Every `byPath` read and write goes
 * through this (rules 51 and 103).
 */
export function cacheKey(path: string): string {
  return path.split(sep).join("/");
}

/** The stored spelling of a path: relative to the analysed root, POSIX (rule 93). */
export function relPath(root: string, abs: string): string {
  let rel = relative(root, abs).split(sep).join("/");
  if (!rel || rel.startsWith("..")) rel = abs.split(sep).join("/");
  return rel;
}

/**
 * The compiler options a tsconfig asks for, with the ones this reader must have.
 *
 * `noEmit` because nothing is being built; `allowJs` off because a `.js` file is another
 * reader's or nobody's, and admitting one here would put facts in the map under a language
 * no manifest claims. Everything else is the project's own — `paths`, `baseUrl`,
 * `moduleResolution` and `jsx` are precisely what decides whether an import resolves.
 */
function optionsFor(configPath: string | null): ts.CompilerOptions {
  const base: ts.CompilerOptions = {
    noEmit: true,
    allowJs: false,
    // A checker that stopped at the first broken `.d.ts` in `node_modules` would refuse to
    // answer about code that compiles perfectly for its own author.
    skipLibCheck: true,
    noResolve: false,
    target: ts.ScriptTarget.Latest,
  };
  if (configPath === null) return base;
  const read = ts.readConfigFile(configPath, ts.sys.readFile);
  if (read.error !== undefined) return base;
  const parsed = ts.parseJsonConfigFileContent(
    read.config,
    ts.sys,
    dirname(configPath),
    undefined,
    configPath,
  );
  return { ...parsed.options, ...base };
}

/** The nearest `tsconfig.json` at or above this file, stopping at the analysed root. */
function projectOf(root: string, abs: string): string | null {
  let dir = dirname(abs);
  for (;;) {
    const candidate = join(dir, "tsconfig.json");
    if (existsSync(candidate)) return candidate;
    const up = dirname(dir);
    if (up === dir || dir.length <= root.length) return null;
    dir = up;
  }
}

export interface Project {
  /** The `tsconfig.json` this group was built from, or `""` for the files under none. */
  config: string;
  /** Absolute paths, exactly as handed to `createProgram`, in the order the host gave them. */
  roots: string[];
}

/** The one project that is built: its program, its checker, and the files it parsed. */
export interface Live {
  project: Project;
  program: ts.Program;
  checker: ts.TypeChecker;
}

/**
 * **One project's compilation is alive at a time, and that is the whole of the memory
 * bound.** Measured on emir, 2026-11-20: sixteen `tsconfig.json` projects, every one of them
 * built in `open` and held to the end, came to 1.85 GB of heap after collection before a
 * single file was extracted, and 3.70 GB once the checkers had answered for 743 of them —
 * past node's 2 GB default, twice, so the reader died, was restarted, and died again. A
 * larger heap is not the fix on an 8 GB machine; it trades a crash for the swap file.
 *
 * So `open` builds nothing, and `visit` builds the one project a request needs and lets the
 * previous one go — program, checker, and every file it parsed — after `beforeRelease`
 * has taken what it needs from it. **Nothing that crosses a project is a compiler object**:
 * the edge pass keys a routine by file and position (`edges.ts`' `anchorKey`), because a
 * `ts.Node` from a released program is a different object from the same declaration in the
 * next one. That is the Java reader's rule — what crosses a batch is a name, never javac's
 * identity (`LANGUAGES.md`).
 *
 * Only TypeScript's own `lib*.d.ts` are kept between projects: they come out of the bundle,
 * every project reads the same ones, and re-parsing `lib.dom.d.ts` sixteen times buys nothing.
 */
export class Session {
  root = ".";
  /** Sorted by `config`, because the order programs are built in decides nothing (rule 66). */
  projects: Project[] = [];
  /** How many programs have been built this session, so a rebuild can be said, not guessed. */
  builds = 0;
  /** Said before each build: on stderr and as a `progress` frame (see `main.ts`). */
  onBuild: (detail: string) => void = () => {};
  /** Called with the live project just before it is let go. The edge pass harvests here. */
  beforeRelease: (live: Live) => void = () => {};

  private live: Live | null = null;
  /** `cacheKey(abs)` -> the project whose root it is. A file is the root of one project. */
  private ownerOf = new Map<string, Project>();
  /** An absolute root -> the string the host named it by, which `order` hands back. */
  private given = new Map<string, string>();
  /** `cacheKey(path)` -> a parsed file, for the live project only; cleared on release. */
  private byPath = new Map<string, ts.SourceFile>();
  /** The standard library, parsed once for the session (see the class comment). */
  private libs = new Map<string, ts.SourceFile>();

  /**
   * Group `files` by project, and build nothing.
   *
   * Returns a line per project for the `opened` frame's `diagnostics`, which has carried
   * the field since chunk 9 and never had anything to put in it. A reader that says how it
   * grouped a monorepo is a reader somebody can check; one that says nothing is one whose
   * resolution rate has no explanation (design rule 1).
   */
  open(root: string, files: string[]): string[] {
    this.release();
    this.root = root;
    this.projects = [];
    this.ownerOf.clear();
    this.given.clear();
    this.builds = 0;

    const groups = new Map<string, string[]>();
    for (const f of files) {
      const abs = resolve(f);
      this.given.set(abs, f);
      const config = projectOf(root, abs) ?? "";
      const bucket = groups.get(config);
      if (bucket === undefined) groups.set(config, [abs]);
      else bucket.push(abs);
    }

    const said: string[] = [];
    if (Object.keys(TS_LIBS).length === 0) {
      // **Said out loud rather than discovered from the numbers.** A program with no
      // standard library resolves, answers, and types every value through `Array`,
      // `Promise` or the DOM as `any`; the map it yields looks perfectly healthy and the
      // run cannot say why half its calls went nowhere (rule 49). It only happens to a
      // bundle built without `node_modules`, which `build.mjs` refuses — so if it is ever
      // read, something built this reader in a way nobody meant to.
      said.push("no TypeScript standard library is carried in this bundle — every value "
                + "typed through Array, Promise or the DOM will be `any`");
    }
    for (const config of [...groups.keys()].sort()) {
      const project: Project = { config, roots: groups.get(config) ?? [] };
      this.projects.push(project);
      for (const abs of project.roots) this.ownerOf.set(cacheKey(abs), project);
      said.push(
        `${this.nameOf(project)}: ` +
        `${project.roots.length} file${project.roots.length === 1 ? "" : "s"}`,
      );
    }
    return said;
  }

  /**
   * Every file, in the order this reader would like to be asked for them: project by
   * project, so each is built once. The host sorts by it (`opened.order`); a request out of
   * this order is still answered correctly, at the price of a rebuild.
   */
  order(): string[] {
    // **As the host spelt them**, because the host sorts its own paths by these and a file
    // it cannot find in the list is simply asked for last — a resolved spelling would be a
    // request nobody can see being ignored.
    return this.projects.flatMap((p) => p.roots.map((abs) => this.given.get(abs) ?? abs));
  }

  /** The project a file is the root of, or `undefined` for one the host never named. */
  projectOfFile(abs: string): Project | undefined {
    return this.ownerOf.get(cacheKey(resolve(abs)));
  }

  /** The project's name as a reader would say it: its tsconfig, relative, or `(no tsconfig)`. */
  nameOf(project: Project): string {
    return project.config === "" ? "(no tsconfig)" : relPath(this.root, project.config);
  }

  /**
   * Make `project` the live one, letting the previous one go first.
   *
   * **The release comes before the build**, or the peak is two projects rather than one —
   * and the two largest of emir's are a third of what killed it.
   */
  visit(project: Project): Live {
    if (this.live !== null && this.live.project === project) return this.live;
    this.release();
    const n = this.projects.indexOf(project) + 1;
    this.onBuild(
      `building the program for ${this.nameOf(project)} (${n} of ${this.projects.length}, ` +
      `${project.roots.length} file${project.roots.length === 1 ? "" : "s"})`,
    );
    const options = optionsFor(project.config === "" ? null : project.config);
    const host = this.compilerHost(options);
    const program = ts.createProgram(project.roots, options, host);
    this.builds += 1;
    this.live = { project, program, checker: program.getTypeChecker() };
    return this.live;
  }

  /** Let the live project go, after `beforeRelease` has had it. Idempotent. */
  release(): void {
    const live = this.live;
    if (live === null) return;
    try {
      this.beforeRelease(live);
    } finally {
      this.live = null;
      this.byPath.clear();
    }
  }

  /**
   * A compiler host that reads through `readSource` and parses through `makeSourceFile`.
   *
   * Both halves are load-bearing. The read is the newline normalisation the protocol
   * requires (`ingest.contract.SOURCE_TEXT`) — without it every CRLF file in the
   * compilation is a different string from the one the host hashed. The parse is
   * `extract.ts`' own, so the tree the checker reasons about and the tree the extractor
   * walks are the **same objects** and a declaration can be matched by node identity
   * rather than by re-deriving a line — inside the one live project. Across projects it is
   * matched by position, because every parse of one text by this one function lays it out
   * the same way (`edges.ts`' `anchorKey`).
   */
  private compilerHost(options: ts.CompilerOptions): ts.CompilerHost {
    const host = ts.createCompilerHost(options, /* setParentNodes */ true);
    const cache = this.byPath;
    const libs = this.libs;
    // **The standard library comes out of the bundle, not off the disk.** A bundled `tsc`
    // resolves `lib.es5.d.ts` beside `__dirname`, which here is the plugin's own directory
    // and holds no such file — and it does not refuse: it builds a program with no `Array`,
    // no `Promise` and no DOM, and answers anyway. `src/libs.ts` has the measurement.
    const libOf = (fileName: string): string | undefined =>
      fileName.startsWith(LIB_DIR) ? TS_LIBS[fileName.slice(LIB_DIR.length + 1)] : undefined;
    host.getDefaultLibLocation = () => LIB_DIR;
    host.getDefaultLibFileName = (o) => `${LIB_DIR}/${ts.getDefaultLibFileName(o)}`;
    host.fileExists = (fileName: string): boolean =>
      libOf(fileName) !== undefined || ts.sys.fileExists(fileName);
    host.readFile = (fileName: string): string | undefined => {
      const lib = libOf(fileName);
      if (lib !== undefined) return lib;
      try {
        return readSource(fileName);
      } catch {
        return undefined;
      }
    };
    host.getSourceFile = (fileName, _languageVersion, onError): ts.SourceFile | undefined => {
      const lib = libOf(fileName);
      const held = lib !== undefined
        ? libs.get(cacheKey(fileName))
        : cache.get(cacheKey(fileName));
      if (held !== undefined) return held;
      let text: string;
      if (lib !== undefined) {
        text = lib;
      } else {
        try {
          text = readSource(fileName);
        } catch (err) {
          if (onError) onError(String(err));
          return undefined;
        }
      }
      // Named by the path the compiler asked for, because the compiler addresses it by
      // that and nothing in the extractor reads `sf.fileName` at all — a stored path is a
      // fact about a file and travels as one, in `FileFacts.path`, never on the AST. The
      // edge pass's `anchorKey` does read it, and reads it through `cacheKey`.
      const sf = makeSourceFile(fileName, text);
      if (lib !== undefined) libs.set(cacheKey(fileName), sf);
      else cache.set(cacheKey(fileName), sf);
      return sf;
    };
    return host;
  }

  /**
   * The source file for one path, **from its own project's program**, built if need be.
   *
   * A file no project holds as a root is parsed on its own, which is what happens when
   * nothing has been opened — the shape chunk 9's tests drive. A file whose text is not
   * what the host just read is refused rather than reconciled: the two passes would then
   * be walking different trees, and every id the edge pass minted for it would name a call
   * site the map does not have.
   */
  sourceFile(abs: string, rel: string, source: string): ts.SourceFile {
    const project = this.projectOfFile(abs);
    if (project === undefined) return makeSourceFile(rel, source);
    const held = this.visit(project).program.getSourceFile(resolve(abs));
    if (held === undefined) return makeSourceFile(rel, source);
    if (held.text !== source) {
      throw new Error(
        `${rel} was read one way for the compilation and another way for extraction. ` +
        `Both passes have to walk one tree, or the ids one mints name call sites the ` +
        `other never made.`,
      );
    }
    return held;
  }

  /**
   * The checker of the project a file is the root of, for the **extraction** pass.
   *
   * Chunk 10 needed a checker only in the edge sweep. Chunk 11c needs one per `extract`
   * request, because a boundary can be a fact about the package a call resolves into
   * (`pkg.ts`) and boundaries are per-file facts — so the answer has to be available where
   * the file is read.
   *
   * **The file's own project, not the first program that happens to hold it.** Until
   * 2026-11-20 this walked every built program and returned the first containing the file,
   * which for a package imported across a monorepo was whichever importer sorted first —
   * another project's compiler options deciding this file's boundaries.
   *
   * `undefined` where nothing has been opened, which is the shape chunk 9's tests drive
   * and is a real answer: no checker, no package rules, and the name rules decide alone.
   */
  checkerFor(abs: string): ts.TypeChecker | undefined {
    const project = this.projectOfFile(abs);
    return project === undefined ? undefined : this.visit(project).checker;
  }

  close(): void {
    // Nothing is asked after `close`, so the project still live is let go unharvested.
    this.beforeRelease = () => {};
    this.release();
    this.projects = [];
    this.ownerOf.clear();
    this.libs.clear();
  }
}
