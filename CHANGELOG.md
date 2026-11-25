# Changelog

What changed between releases, for somebody deciding whether to upgrade and what it will
cost them. The session-by-session record of how each thing was found is kept outside this
repository; what is still owed is [`BACKLOG.md`](BACKLOG.md).

## 0.3.1

Two crashes fixed, and maps that would not travel between Windows and the rest. Nothing else changed.

### Before you upgrade

- **Every map made by 0.3.0 has to be analysed again.** The TypeScript fix below changes
  how the host drives a reader, and that code is part of every map's stamp. So 0.3.1
  refuses a 0.3.0 map, in any language, and says so. `analyze` rebuilds it from the tree.
  A map that is not re-read stays refused, and nothing else is lost. Time it by the
  original analysis: seconds for a small program, about sixteen minutes for OpenUSD, about
  three hours for Unreal's `Engine/Source/Runtime`.
- **0.3.1's dashboard and MCP server rewrite two bytes of a map journalled by WAL before
  reading it.** That journal is what the crash below comes from. 0.3.1 writes no such map,
  but an older writer did, and so does a 0.3.1 writer if something held the file open as it
  finished. No row changes and no stamp moves. A map that fails its checks is refused
  untouched. A map that something else holds open at that moment (an analysis still writing
  it, another server) is left as it is, and is read as before. The command-line readers
  (`touches`, `query`, `check` and the rest) never rewrite a map.

### Fixed

- **The dashboard died when the Mac slept with a map open on an external drive.** Sleep
  dismounts the drive, and the server was killed by a bus error (SIGBUS) on its next
  request, with nothing on screen to say why. A map is no longer read through shared
  memory, so a disconnected drive is now an error the server can answer. While the drive
  is away the page says which file it cannot read. When the drive is back, the server
  reopens the map by itself and the page carries on where it was, with no restart. The
  MCP server does the same. `tests/test_volume_loss.py` detaches a real disk image under a
  reader to prove it.
- **Analysing a large TypeScript monorepo ran out of memory.** The reader held every
  `tsconfig.json` project's compilation at once: 3.7 GB of heap on a 16-project tree,
  against node's 2 GB default, so the reader died twice and the job failed. It now holds
  one project at a time (TypeScript reader 1.4.0): that tree reads with a peak of 1.33 GB,
  and its map is identical to one read with an 8 GB heap. While the reader works, the
  progress line says which project it is on. If a reader does die, the message says what
  it was doing and quotes any out-of-memory line it printed.
- **A C, C++ or Rust map made on Windows was refused on a Mac or Linux, and the other way
  round.** The readers' stamps were hashed in an order that Windows works out without
  regard to case, so the same reader gave two stamps. Maps are now stamped alike on every
  platform.
- **The document and source stores opened "read-only" with a read-write connection.**
  They now open read-only, as the graph always has (formerly BACKLOG 62).

### Known limits

- An analysis writing its map to a drive that sleep dismounts still dies. It runs in its
  own process, so the dashboard that started it survives and reports the failure.
  Analyse again once the drive is back.
- The Analyse progress bar counts steps, so it stays empty through the first one, as in
  0.3.0.

## 0.3.0

### Before you upgrade

- **A map made by 0.2.0 has to be analysed again.** The store went from schema 15 to 20
  and there is no migration between them. Opened for reading, an old map is refused with
  a sentence saying so. Opened by `analyze`, it is emptied and rebuilt from the tree. A
  snapshot sealed for comparison under 0.2.0 cannot be compared against a 0.3.0 one; seal
  a new pair.
- **`packaging/release.sh` no longer has a default signing identity.** Set
  `DUNNEFLOW_IDENTITY` before `sign` or `dmg`.
- **A script that POSTs to the dashboard must send `X-DunneFlow: 1`**, and must address
  it as `127.0.0.1` or `localhost`. The page does both by itself.

### New readers

- **Go**, on `go/types`. Interface calls name the bodies that may answer; a method is a
  boundary by its receiver's type (`os.Root`, `net.Dialer`); Cobra's command tree is drawn.
- **Rust**, on `syn`, vendored so nothing is fetched. Trait calls name the bodies that may
  answer. A Tauri command is a way in, and the front end's `invoke("name")` is linked to it
  across the two languages.
- **Java**, on javac's public tree API. Dispatch is said as dispatch and never guessed.
  Dependencies are read from this machine's Maven and Gradle caches where they are there,
  and never fetched. A tree of any size is attributed in batches.
- **C and C++ on libclang**, replacing the token reader. Macros are followed to what they
  expand to; a way in marked on a declaration is bound to its definition by the compiler's
  own identity for it.

### New in every language

- **`LOCAL_IPC`**: a Unix-domain socket is another process on this machine, and is no
  longer counted as the network.
- **Every unresolved call says where it stops**: outside the program and where, untyped
  and why, or dispatched and to how many bodies. Reach, `touches`, `impact` and
  `UNREACHABLE` cross a dispatched call into each body that may answer it.
- **What a type says and no spelling does** (Java): a call on a Spring Data repository is
  a database touch, a JAX-RS client call is a network touch where its jar is on the
  machine, and a method that fills a servlet's or Netty's slot is a way in.
- **A boundary a signature names** is shown beside what a body touches, and is never
  counted as a touch.
- A command no client sends is reported as dead code is. A map no model touched says
  nothing about models.

### Fixed

- **A Python call was drawn to the wrong routine when the program had exactly one of that
  name.** `rows.append(x)` was shown as a probable call to the program's only `append`,
  and likewise `get`, `update`, `count` and the other methods of Python's own types. On
  this tool's own map that was 893 of 1,146 inferred edges. Such a call is now resolved
  only by evidence (its class, a base class, an import) and is otherwise said to be
  untyped. A few true edges are lost with the false ones; see Known limits.
- **Code mode kept saying "reading what behaves differently…"** when the comparison
  answered after the file had been drawn. It now repaints when the answer arrives.
- **A C or C++ program's own function was counted as the library's when it shared its
  name.** A call to `send(frame)` in a program that defines `send` was drawn as a call to
  that routine and also listed as a NETWORK touch. This tool's own map said NETWORK 17
  where three are real. A bare name whose body the compiler has in the same translation
  unit is now the program's and nothing else. A body in another file is left as it was,
  because that is how a compatibility shim for another platform looks; see Known limits.
- **Two controls in the Analyse dialog were drawn by the browser** and not by the
  dashboard: the list under *Under each flow diagram* and the minutes box, white in the
  dark theme.
- **Five things in the dashboard that the retake of its own screenshots turned up.** With
  a file already open beside the navigator, a search that answered left the way back
  reading *‹ files*; it now says what the search found. A call with no edge says why in
  words, and those words ran off the inspector and took the EXTERNAL badge with them; they
  wrap under the name now. A long path stood outside its *Worth a look* card. Diagram
  opened on the largest compartment, and said *what you picked is not in this
  compartment*, when a routine was picked before the stack had been drawn in that page.
  And the stack's **clear** button stayed up after it was pressed.

### Security

Reviewed as a whole for the first time; [`SECURITY.md`](SECURITY.md) says what is
defended and what is not. Five things were confirmed and fixed:

- The dashboard answered requests carrying another site's `Host` or `Origin`, so a page
  open in the same browser could read a map or start an analysis. Every request is now
  checked, and every response carries a content security policy.
- A reader's toolchain was started in the directory `analyze` was run from, where a
  `go.mod` naming a newer Go made `go` try to download and run it.
- A symbolic link out of the tree was read like any file, and a named pipe hung the
  analysis. Only files of the tree are read, and what is passed over is counted.
- `mcp`'s `open` took a path as a program name, and its `query` had no time limit.
- `query --docs` could write to the document store.

And three were not confirmed, and hardened all the same: what a `compile_commands.json`
may pass to clang, how a program's text is escaped on the dashboard, and what the lint
refuses to let into this codebase.

### Known limits

In [`BACKLOG.md`](BACKLOG.md), each with what would settle it: Go dependencies read as
*unknown* rather than *outside* (32); `--include-tests` does not bring back Rust's inline
test items (37); four rules files hold a common method name that needs measuring (38); a
Kotlin file in a Java tree is a missing class to javac (47). A Python method that shares a
name with one of Python's own, called on a value nothing types, reads as untyped (stated
residues). A C function the tree defines in one file and calls from another under a
library's name reads as both (stated residues). The Windows and Linux builds are unsigned,
and each archive is published with its SHA-256.

## 0.2.0

The second line of DunneFlow: the same tool as 0.1.0, rebuilt so that reading is modular.
TypeScript, C++ and Kotlin beside Python, the MCP server, and reading very large trees in
parts.

## 0.1.0

The first release. Kept on the `v1` branch and the `v1-final` tag.
