# Test plan

Run everything with `./run-tests.sh --all` (about 1½ minutes). Current state: 635 automated tests. On Windows (`run-tests.bat --all`, about 6 minutes)
the 117 bash-script tests are skipped, and 85% of the code is reached (`--cov` shows the rest).

## How the tests are built

- **No real AI model is needed.** Face scanning is tested with a scripted fake engine, so tests are fast and
  repeatable. Face grouping is tested on synthetic "people" (known identities plus noise).
- **Every test works in a throw-away folder** with its own database; your real library and index are never touched.
- **The bash scripts run for real**, but against a fake Python that only records what it was asked to run.
- **Browser tests** start the real web interface and click through it with Chromium (Playwright).

## What is covered, file by file

| Area | Test file | Scenarios |
|---|---|---|
| Date choice | `test_dates.py` | EXIF taken / digitized / modified order, malformed and zero dates, missing EXIF, file time fallback, future dates, non-photo files |
| Sorting | `test_sort.py` | dry run changes nothing; copy vs `--move`; `Year/NN-mon` names for every `--month-format`; same-name collisions; identical duplicates; re-running is safe; unreadable files; destination inside source; legacy `SRC DEST` form; trash folder skipped |
| Refresh | `test_refresh.py` | index rebuilt from a sorted library; new, moved, renamed, changed and missing files; `--keep-missing`, `--force-prune`, `--rehash`; `--adopt` re-points photos to a copied library (same rows, old files untouched) but leaves extra copies inside one library alone; refusing to prune when the library looks empty or offline |
| Index and info | `test_schema_info.py` | table creation, upgrades from older databases, foreign keys / cascades, `info` output |
| Command line | `test_cli.py` | every command and flag, exit codes (0, 1, 2, 130), `--db`, `-v`, `--log-file`, usage errors |
| Face scan | `test_face_scan.py` | scan, `--limit`, `--rescan`, `--retry-errors`, `--discard-confirmed`, no-face photos, corrupt images, engine failures, resuming after Ctrl+C, GPU/CPU choice, model problems |
| Grouping | `test_face_cluster.py` | clean groups, strangers, tiny/blurry/side-on faces go to review, merging, thresholds, `--recluster`, named people never changed, idempotence, scanned photos without any faces is "nothing to group" (not an error) |
| Web interface (API) | `test_ui_server.py` | people, naming, merging, moving faces, ignore / restore, review queue, rejections, security (host check, required header, no-network default) |
| Photos page (API) | `test_photo_browse.py` | listing and paging, every quick filter, year / month / text search (including a literal `%`), person filters (all / any, uncertain), thumbnails and caching, rename rules (Windows-illegal names, clashes, case-only, extension kept), delete to trash, bulk delete (partial failure, limits, duplicates), cleanup of orphaned people |
| Metadata (phase 6) | `test_metadata.py` | tables, triggers and upgrade of an older index; camera details (make/model, lens, exposure, GPS north/south/east/west, zero GPS, rotated photos, no EXIF, bad values, unreadable files); `sort`, `refresh` (new photos, catch-up for older ones, `--no-metadata`, other libraries untouched, notes survive a move) and the `metadata` command (preview, `--limit`, `--reread`, missing and unreadable files, Ctrl+C and resume); `info`; notes (one per photo, replace, clear, too long), model annotations never overwrite yours; embeddings (32/16-bit, replace, damaged rows, mixed sizes); search (names, folders, partial names, note prefixes, punctuation, accents, edits, renames, without FTS5); the photo API (detail, saving notes, header check, search and *Has notes* filter, delete and rename) |
| Search by description (phase 7) | `test_clip_search.py` | `clipcheck` with a tiny generated ONNX model: passing and failing picture/word checks, timings and the estimate from the index, missing model, unsafe model names, missing packages, `--gpu` without CUDA, damaged files, `--list`; downloads (every file, only missing ones, unknown model, offline, cut short, Ctrl+C then resume, full disk, generic User-Agent); preprocessing (crop, squash, pad, EXIF rotation, reduced JPEG decoding), fixed-batch models, tokens and padding, SigLIP text cleaning, unsupported multilingual models; `embed`: preview writes nothing, 16-bit unit vectors that match each photo's content, `--limit` then resume, `--again`, other models untouched, one-picture-at-a-time models, no index / model not downloaded / missing packages / unsafe name / `--gpu` without CUDA / damaged model, missing and unreadable photos retried later, the model failing repeatedly, Ctrl+C keeps whole batches only, refusing to mix vector sizes, vectors removed with their photo; web search: best match first, combined with the other filters, paging and the best-N choice, photos without a vector counted, new and replaced vectors picked up, deleted photos gone, the models list (usable or not), no vectors yet, unknown or missing model, missing packages, mixed vector sizes, damaged model, the text half loaded once and only on the CPU |
| Tasks page (phase 7.5) | `test_ui_tasks.py` | folder browser (sorted, hidden folders left out, shortcuts, remembered folders, missing / relative / quoted paths); sorting through the real command (preview changes nothing, then copy and index, move, every folder check, the page sees the new photos, folders remembered); `sort` stopped half way keeps finished photos and leaves no partial copy; one task at a time (also against Place new faces); Stop finishes the current step cleanly and skips the rest; faces runs refresh, scan, group with the chosen limit and hardware, goes on after some problems but not after a failure; search preview, download only when agreed, a failed model check stops before processing, downloaded models listed; bad model names; the log keeps the newest lines; the required header; the photo library: none chosen yet, a folder that doesn't exist yet, preview then add an already-sorted library (counts of indexed / scanned / searchable photos), switching to another library keeps the old photos and takes over copies, add runs index, faces, grouping and search (download only when agreed; faces and search independent, a failed index update stops the rest), no change while a task runs |
| Starting the interface | `test_ui_launch.py` | no index yet (an empty one is made), busy port, network exposure warning, Ctrl+C, browser opening, corrupt index |
| Bash scripts | `test_shell_scripts.py` | menu (every choice, bad input, end of input), option 2 preview → confirm → apply, option 3 refresh → faces → cluster with every stop/continue branch, option 4 camera details preview → confirm → read (re-read, no index, errors, Ctrl+C), option 5 search preview → (download the model) → confirm → store vectors (remembered model, download declined / failed / stopped, no index, errors, Ctrl+C, missing `tokenizers`), option 6 list → check → (download after asking) with the passing model remembered for option 5, remembered folders, quoted paths, Python discovery, `run-tests.sh` flags, `deploy.sh` placeholder refusal, dry run, backups and pruning, stale-file removal, rollback, data/config safety |
| Windows scripts | `test_windows_scripts.py` | the same menu, test-runner and deploy scenarios for `scripts/menu.py`, `run_tests.py`, `deploy.py` (what the `.bat` files start); `.bat` files have Windows line endings and call the right script; `photo-sorter.bat` run for real with `cmd` and git against a local stand-in for GitHub: updates to `main` and restarts from the new copy (also when the update changes the `.bat` file itself), lists changed package lists, passes the menu's exit code on, and leaves local changes, local commits, other branches, no network and non-git folders alone; `PHOTO_SORTER_NO_UPDATE` |
| Browser | `e2e/test_browser.py` | Photos page, search by description (greyed out until prepared, best match first, no month headings, note about photos without vectors, best-N choice, viewer, clear), naming a person then filtering by their chip, viewer keyboard navigation, rename and delete with confirmations and cancel, writing a note and finding the photo by it, multi-select delete, People / Review / Tasks pages, the Tasks page (choose a folder by clicking through the folder browser, preview, then copy the photos into the library; change the photo library and add it), no sideways scrolling on a phone, no JavaScript errors |

## Not automated: check these by hand

Do these once on the real machine, and again after a big change.

1. **GPU.** `python sort_photos.py gpucheck` reports the GPU provider; a scan with `--gpu` is faster than `--cpu` and finds the same faces.
   `python sort_photos.py clipcheck --download` with each suggested model: 3 of 3 right, runs on the NVIDIA GPU, note the timings.
   Then `python sort_photos.py embed --apply --limit 50`: says it runs on the GPU, about the speed `clipcheck` predicted,
   and a second run continues with the rest.
2. **Real face quality.** On about 200 of your own photos: do the groups look right, are strangers kept out, are side-on and small faces sent to review rather than mixed in? Adjust thresholds if not.
3. **Network drive.** Sort 20 photos onto the drive in copy mode, then move mode. Confirm names, dates and that originals survive in copy mode. Unplug / disconnect mid-run and confirm a clean error, then a clean re-run.
4. **Windows.** Run `photo-sorter.bat` from Command Prompt (the `.bat` launchers can only be exercised on Windows; their Python scripts are tested automatically) and `./photo-sorter.sh` from Git Bash. Try a path with spaces, a drag-and-dropped (quoted) path and a `Z:\` path for options 2 and 3.
5. **Large library.** With your real library (tens of thousands of photos): refresh time, memory use, Photos page scrolling, thumbnail creation speed.
6. **Deploy.** `./deploy.sh --dry-run`, then a real deploy to a spare folder, start it from there, then `--rollback`.
7. **Browsers.** Open the interface in your everyday browser, on your phone (if you use `--host 0.0.0.0`) and check rename, delete and multi-select.
8. **Tasks page.** From `photo-sorter.bat` option 1, sort a few new photos from a memory card or phone folder with
   **Browse…** (try a network drive letter too), then **Find faces in them** with the GPU, and **Stop** a long
   face scan part way: the page says Stopped, and starting it again continues. Change the photo library to a copy of
   your library on another drive and **Add this library**: named people keep their photos, nothing is scanned twice. Close the menu window while a task
   runs and check in Task Manager that no `python` process is left behind.
9. **Search quality.** After `embed` with both models, search the Photos page for ten things you know are in your
   library ("birthday cake", "beach", "snow", a pet, a car...) with each model and note which finds them higher up.

## Adding a test

Put it next to the area it belongs to. Use the fixtures in `tests/conftest.py`: `env` (private folders), `cli`
(runs a command and returns `.code .out .err`), `face_index` (synthetic people) and `ui` (a client for the web interface).
