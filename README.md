# Photo sorter

## Phase 1: date-based sorting (this folder)

Setup (Windows PowerShell):

    py -3.11 -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

Run (always test on a COPY of a small folder first):

    python sort_photos.py "C:\Photos\Test" "C:\Photos\Sorted"            # dry run, changes nothing
    python sort_photos.py "C:\Photos\Test" "C:\Photos\Sorted" --apply    # copy into Year\Month folders

Options: `--move` (verified move instead of copy), `--month-format abbr|full|number|name`, `-v` (detailed output), `--log-file FILE`.

Result: `Sorted\2023\03-mar\IMG_0001.jpg`, plus a local index at
`%LOCALAPPDATA%\photo-sorter\photo_index.db` (SQLite; records each photo's
hash, original path, sorted path, date and date source). The index is kept on
your local disk on purpose: SQLite is unreliable on network drives. Later phases add face data to this same database.

## Where is the database? Logging

The index is a SQLite file on your local disk, default
`%LOCALAPPDATA%\photo-sorter\photo_index.db`. Every run prints its exact location
at the start and end. To check it any time without running anything:

    python sort_photos.py info        # path, size, number of photos, date range

Output levels (all commands):
- Default: key steps (settings, index location, scan, progress, summary, warnings).
  Dry runs also list every file; applied runs list only progress and the summary.
- `-v`: detailed debug output with timestamps (hashes, chosen dates, name clashes,
  index writes, retries).
- `--log-file C:\path\run.log`: also saves the full debug log to a file, handy for
  long runs on a network drive. Warnings and errors go to stderr.

## Refresh mode: rebuild the index from an already-sorted library

Use this when you already have sorted folders (or the index was lost/moved to a
new PC), and before running face recognition on existing photos:

    python sort_photos.py refresh "Z:\Photos\Sorted"                  # preview
    python sort_photos.py refresh "Z:\Photos\Sorted" --apply          # update the index (also removes deleted photos)
    python sort_photos.py refresh "Z:\Photos\Sorted" --apply --keep-missing  # update, but keep rows for deleted photos

What it does:
- New files are added to the index (dated from EXIF, else file date).
- Moved or renamed files are matched by content hash and their path is updated.
- Extra copies of the same photo are reported as duplicates and never touched.
- Photos that were deleted from disk are removed from the index, together with their face
  records. The preview shows what would be removed, including how many face records go with it.
  A photo that was only moved or renamed is not "missing": it is matched by content and its path
  is updated. Use `--keep-missing` to leave rows for deleted photos in the index.
- Safety guards, because your library is on a network drive: nothing is removed if the scan finds
  no photos or hits errors (drive disconnected or unreadable), or if 10 or more photos are missing
  and that is more than half of this library's indexed photos (that looks like a disconnected or
  damaged folder, not deliberate deletions). In that case it warns and changes nothing; if you
  really did delete them, re-run with `--force-prune`.
- Only photos under the library folder you pass are considered, so refreshing one folder never
  removes photos that belong to another.
- Files whose path, size and modified time are unchanged are not re-hashed, so
  repeat refreshes of a large library are fast. `--rehash` forces a full re-hash.
  The first refresh of an index created by an older version hashes everything once.

It never moves, renames or deletes your photo files (it only updates the index). Like sorting, it is a dry run until `--apply`.
`sort_photos.py SOURCE DEST` still works as before (`sort` is the default command).

## Phase 2: find faces (offline)

The AI only finds faces and gives each one a numeric "fingerprint" (an embedding). It
never names anyone. Phase 3 groups matching fingerprints; phase 4 is where you name them.

Extra setup (once, inside the same virtual environment):

    pip install -r requirements-faces.txt

Run:

    python sort_photos.py faces                      # preview: how many photos would be scanned
    python sort_photos.py faces --apply --limit 20   # try it on 20 photos first
    python sort_photos.py faces --apply              # scan everything not yet scanned

It scans the photos listed in the index, so run `sort ... --apply` or `refresh ... --apply` first.
Useful options: `--limit N`, `--rescan` (redo everything), `--retry-errors`, `--gpu`, `--cpu`,
`--det-size 960` (finds smaller faces in group photos, slower), `--min-score`, `--max-side`,
`--model buffalo_s` (faster, a little less accurate), `--model-root`, plus `-v` and `--log-file`.

What to expect:
- The first real run downloads the model (about 280 MB) to `C:\Users\<you>\.insightface`.
  After that it works fully offline. The model is licensed for non-commercial use, which
  covers a personal photo library.
- Scanning is resumable. Every photo is saved as soon as it is done, so Ctrl+C (or a crash)
  loses nothing; run the same command again to continue. Photos that cannot be read are
  recorded as errors and skipped until you pass `--retry-errors`.
- Phone photos are rotated correctly (EXIF orientation is applied before detection).
- It uses your NVIDIA GPU when it is set up (next section) and the CPU otherwise. The log says which.
- For each face the index stores: position, detection score, size in pixels, a sharpness
  number, head turn (yaw), the 512-number embedding and a small 128 px thumbnail, so the
  naming UI never has to re-read your originals. Face fingerprints identify real people:
  keep the index on your local disk and out of cloud sync.
- `python sort_photos.py info` shows how many photos are scanned and how many faces were found.
- Photos deleted from disk are removed from the index, with their face rows, by `refresh --apply`.
  The `faces` command skips deleted photos (it does not record them as errors) and tells you to run `refresh`.

### Using your NVIDIA GPU

Face scanning picks the GPU automatically once ONNX Runtime's GPU build is installed. By default
you get the CPU build, because InsightFace installs it, and the two builds cannot be installed
together. In your virtual environment:

    pip install -r requirements-faces.txt
    pip uninstall -y onnxruntime onnxruntime-gpu
    pip install "onnxruntime-gpu[cuda,cudnn]"

The `[cuda,cudnn]` part installs the CUDA and cuDNN libraries through pip, so you do not need to
install the CUDA toolkit yourself. Then check it:

    python sort_photos.py gpucheck

`gpucheck` shows your GPU and driver, whether ONNX Runtime can really start on it, and times a
sample photo on the GPU against the CPU. After that:

    python sort_photos.py faces --apply --gpu    # --gpu stops with an explanation instead of quietly using the CPU

Good to know:
- ONNX Runtime quietly falls back to the CPU when it cannot start the GPU. The scan checks which
  hardware each model really runs on: normally it warns and carries on with the CPU, and with
  `--gpu` it refuses to run. `--cpu` forces the CPU.
- The current `onnxruntime-gpu` is built for CUDA 13. That needs an up-to-date NVIDIA driver and,
  as far as I can tell, a GTX 16-series / RTX 20-series or newer card (CUDA 13 dropped support for
  Pascal and older, such as the GTX 10-series). For an older card or driver, use the CUDA 12 build
  instead, replacing the last command above with: `pip install "onnxruntime-gpu[cuda,cudnn]==1.26.0"`.
- GTX 10-series (Pascal) and older also need an older cuDNN: cuDNN 9.9 and later dropped those
  cards. The symptom is `gpucheck` loading the models on the GPU and then failing with
  `CUDNN_STATUS_EXECUTION_FAILED_CUDART` or `cudnn_engines_... .dll` errors. After the CUDA 12
  command above, also run: `pip install "nvidia-cudnn-cu12<9.9"`. A newer driver alone does not fix it.
- Windows wheels exist for Python 3.11 and 3.12.
- With a GPU, a larger `--det-size` (for example 960) costs little and finds smaller faces in
  group photos. The scan reads photos in background threads, so a network drive is usually the
  limit once the GPU is working.

## Phase 3: group faces into people

    python sort_photos.py cluster            # dry run: shows what it would do, saves nothing
    python sort_photos.py cluster --apply    # save the grouping

This groups faces that look like the same person. It never names anyone (you do that in the web
UI in phase 4) and it only needs numpy, not a GPU. 30,000 faces take a few seconds.

How each face is labelled:

| Label | Meaning |
|---|---|
| `auto` | Confident: good quality, similar enough to the person, and a clear winner over the runner-up |
| `review` | The AI is unsure. Its best guess is saved as a suggestion. These are what the UI shows you the next time you open it. Reasons: `borderline` (similarity only just enough), `ambiguous` (two people look alike), `low_quality` (small, blurry, turned or low-confidence face) |
| `unassigned` | No good match, usually a stranger in the background. Not in the review queue, but the UI can list them |
| `confirmed` | You checked it in the UI. The AI never changes these |

Running it again is safe:
- By default only new faces and still-uncertain faces are placed. Every `auto` face stays where it is, and
  person numbers stay the same, so run it after each `faces --apply`.
- `--recluster` lets the AI regroup its own work from scratch. People you named and faces you confirmed
  are always kept, along with the faces already in a named person's group. New faces are matched against them.
- Everything is saved in one transaction: an interrupted run changes nothing.
- Two existing people are never merged automatically (merging is your call in the UI). New groups that look
  like the same person are merged.
- "This is not person X" answers from the UI are stored and respected: that face is never suggested for X again.
- `faces --rescan` replaces a photo's face rows, which would erase your confirmations for it, so it refuses
  to touch photos with confirmed faces unless you add `--discard-confirmed`.

Tuning. The defaults are a careful starting point, not tuned on your photos:

| Option | Default | Raise it when... / lower it when... |
|---|---|---|
| `--threshold` | 0.50 | Raise if different people end up in one group; lower if one person is split across many groups |
| `--review-threshold` | 0.35 | Lowest similarity at which a face is even suggested for a person |
| `--merge-threshold` | 0.60 | Lower to merge split groups more aggressively |
| `--margin` | 0.06 | Raise to send more look-alike cases to review |
| `--min-group-size` | 3 | Smaller new groups are treated as one-off faces |
| `--min-det-score`, `--min-face-px`, `--min-blur`, `--max-yaw` | 0.60, 50, 20, 45 | What counts as a good-quality face |

The run ends with a summary, including the similarity of the auto faces, so you can see how tight the groups
are. If many faces land in `review`, check a few groups in the UI, adjust the options and run again with
`--recluster`. Children change a lot as they grow, so expect one child to appear as two or three groups
(different ages); the UI will let you merge them.

## Phase 4: the web interface (name people, review uncertain faces)

    pip install -r requirements-ui.txt       # once: fastapi + uvicorn
    python sort_photos.py ui                 # opens http://127.0.0.1:8765 in your browser

Keep the `web` folder next to `ui_server.py`. Close the window running the command (or press Ctrl+C) to stop it.
Everything stays on this computer: the page only talks to this program, and the program only talks to the index.

**Signing in.** The web interface has one user, **admin**. The first time it is opened, the page asks you to choose
the password (at least 8 characters); after that every browser signs in with it, and stays signed in for 30 days or
until **Sign out** (bottom left). Forgot it? On the computer running the photo sorter, choose **7) Reset the web
interface password** in `photo-sorter.bat` (or run `python sort_photos.py password`): it asks for a new one and
signs every browser out. `python sort_photos.py password --clear` removes it instead, so the page asks again. The
password is kept in the index as a salted PBKDF2 hash, so copying the index to another computer takes it along. After
10 wrong passwords from one address, signing in from there waits ten minutes.

When you open it, you land in **Review** if the AI has questions for you, otherwise in **People**.

- **People:** one contact strip per person (four faces, so a wrong group is obvious at a glance). Type a name under the
  strip. Search, or filter to unnamed people. Typing a name that already exists offers to merge the two groups.
- **A person:** every face in the group, weakest matches first (the likeliest mistakes). Click faces to select them
  (shift-click for a range), then **Move to…**, **Not this person**, **Ignore** or **Confirm they're right**. The
  expand button on a face shows the whole photo with the face outlined, and where the file is. **Might be the same
  person** lists groups that look alike (a child at two ages, for example) with a one-click merge, and
  **Merge into another person…** merges this group into one you choose.
- **Review:** faces the AI wasn't sure about, grouped by who it thinks they are. Select the ones that are right and
  **Confirm as …**; select the wrong ones and choose **Not …** (the AI will never suggest that person for those faces
  again) or **Move to…** the right person.
- **Unassigned:** faces with no match, mostly strangers. Move the ones that matter to a person, **Ignore** the rest.
- **Ignored:** faces you dismissed. They are never placed again unless you restore them.
- **Place new faces** (bottom left) runs the phase 3 grouping for new and uncertain faces, so after you name a few
  people the remaining faces are matched against them.

Your decisions are protected: named people, merged people, confirmed faces and ignored faces are never moved by
`cluster`, even with `--recluster`. Moving faces always counts as confirmed by you. Unnamed groups that become empty
are removed. There is no undo button; to reverse a move, move the faces back.

Privacy and safety:
- Every page and request needs you to be signed in, and every change needs a header that other websites can't send,
  so a web page you visit can't read or change your face data.
- By default it only answers on this computer (`127.0.0.1`). `--host 0.0.0.0` lets other computers on your network
  open it (they still need the password; the program prints the address to use). Windows Firewall also has to allow
  the port, for example in an administrator PowerShell:
  `New-NetFirewallRule -DisplayName "Photo sorter (8765)" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private -RemoteAddress 192.168.68.0/22`
  (use your own network's range; "LocalSubnet" did not cover every machine on a mesh network). It is plain HTTP, so
  keep it inside your home network: don't forward the port on your router.
- `--port` changes the port (default 8765), `--no-browser` stops it opening a browser tab.

### Phase 5: browse the whole library (the **Photos** page)

The first item in the left menu shows every photo in the index, newest first, grouped by month.

- **People quick links:** every person you have named appears as a chip with their photo count. Click one to see only
  their photos; click several to narrow further. With two or more selected you choose *In the same photo* (default) or
  *Any of them*. *Include matches the AI is unsure about* adds faces still waiting in the Review queue.
- **Quick filters:** *Unlabeled faces* (photos with a face nobody has named), *Needs review*, *No faces*, and *Not scanned
  yet* (run `faces --apply` to scan them). A filter is hidden when nothing matches it.
- **Narrow by** year and month, file or folder name, and newest/oldest first.
- **Open a photo** to see it large with a box around each face. Named faces are labelled and clickable ("Show their
  photos"); the side panel lists who is in the photo, where the file lives and when it was taken. Use the arrow keys or
  the Previous / Next buttons to move through the current results, **Esc** to close.
- The address bar holds the filters (for example `#/photos?person=3&year=2019`), so you can bookmark a view. A person's
  page has a **View photos of ...** button.

**Select several photos.** Tick the circle in a photo's corner (Shift-click ticks a range). Once anything is ticked, clicking
a photo ticks it instead of opening it. A bar at the bottom offers *Select all shown*, *Clear* (or press Esc) and
**Delete N photos**, which asks for confirmation with a preview of what you chose, up to 500 at a time. Deleted photos are
handled one by one, so a problem with one file doesn't stop the rest.

**Rename and delete.** In the photo viewer, **Rename** and **Delete photo** each open a confirmation popup first.
- *Rename* changes the file on your drive. It stays in the same folder and keeps its extension (type just the new name),
  and it refuses names Windows doesn't allow or that already exist in that folder.
- *Delete photo* does **not** erase the file. It moves it to a `.photo-sorter-trash` folder at the top of your library
  (a note of each file's original location is added to `deleted.log` there) and removes the photo and its face matches from
  the Photo sorter. Unnamed groups left with no faces are cleaned up. To restore a photo, move it back into its
  Year\NN-mon folder and run `refresh`; to free the space, delete the trash folder yourself. `sort` and `refresh` ignore it.

Thumbnails are made the first time you look at a photo and cached in a `thumbs` folder next to the index database
(`%LOCALAPPDATA%\photo-sorter\thumbs`), so they are quick afterwards. The first screen of a large library on a network
drive can take a little while to appear. Your original photos are only ever read. Deleting the `thumbs` folder is safe.

### Phase 6: camera details, notes and search

The index now keeps information about each photo that is not about faces:

- **Camera details** (make, model, lens, aperture, shutter speed, ISO, focal length, pixel size and GPS position) are
  read when `sort` or `refresh` adds a photo, from the same file read that finds its date, so it costs nothing extra.
  For photos indexed before this existed, the next `refresh --apply` reads them once (add `--no-metadata` to skip
  that). You can also do it on its own with menu option **4** (`photo-sorter.bat metadata`), or from the command
  line, in steps if your library is big:

      python sort_photos.py metadata                    # preview: how many photos have no camera details yet
      python sort_photos.py metadata --apply --limit 500
      python sort_photos.py metadata --apply            # the rest (Ctrl+C is safe; run it again to continue)
      python sort_photos.py metadata --apply --reread   # read every photo again

  The photo viewer shows them under the file path. Rotated photos report their upright size.
- **Your notes.** The viewer has a *Your note* box ("Grandma's 80th, Lake Placid"). It saves when you click *Save note*,
  press Ctrl+Enter, move to another photo or close the viewer. Clearing the box removes the note.
- **Search.** The search box on the Photos page now matches file names, folders **and notes** (every word you type must
  match; note words match from their start, so `plac` finds *Placid*, and accents are ignored). The *Has notes* quick
  filter shows every photo you wrote a note on.
- `info` reports how many photos have camera details, GPS positions and notes.

Notes, camera details and (later) tags, captions and search vectors belong to the photo's entry in the index. They
follow the photo when `refresh` notices it was moved or renamed, and they are removed with it when the photo is deleted
in the web interface or removed by `refresh` because the file is gone (just like its faces).

For later phases the index also has a place for search vectors (`photo_embeddings`, one per photo per model) and for
tags, captions and text found in photos (`photo_annotations`). Anything a program writes there is labelled with that
program's name, and re-running it replaces only its own rows, so your notes are never overwritten. Searching uses
SQLite's built-in full-text index; if your Python's SQLite lacks it, search still works, just more slowly.

### Phase 7: searching by description

Find photos by typing what is in them ("birthday cake", "dog on a beach", "snow on the car"). It uses a CLIP-family
model that runs offline on this computer, through the same ONNX Runtime as face scanning. Three steps:

1. **Check a model** (`clipcheck`, menu option **6**): download one and confirm it works on your GPU.
2. **Store search vectors** (`embed`, menu option **5**): read every photo once. Run it again after adding photos.
3. **Search** on the Photos page of the web interface: type into **Describe a photo**.

#### Check a model

`clipcheck` lets you try models on your computer before choosing one:

    pip install -r requirements-search.txt                 # adds 'tokenizers'; uses the ONNX Runtime you already have
    python sort_photos.py clipcheck --list                 # suggested models, and which are downloaded
    python sort_photos.py clipcheck --download             # download the default model (about 1.7 GB), then check it
    python sort_photos.py clipcheck --model ViT-B-32__openai --download   # a smaller, faster one to compare

Models come from the Immich project's ONNX exports on Hugging Face (`huggingface.co/immich-app/<model>`) and are kept
in a `models` folder next to the index (`%LOCALAPPDATA%\photo-sorter\models`), never on the network drive. Nothing is
downloaded without `--download`; an interrupted download continues where it stopped when you run the command again.

`clipcheck` then:

- loads the model and says whether it runs on the NVIDIA GPU or the CPU;
- checks that pictures and words line up (plain red, green and blue pictures must match "red", "green" and "blue");
- times opening a 12-megapixel JPEG, encoding pictures on the GPU (and on the CPU for comparison) and turning a search
  into a vector (the wait each time you search);
- estimates how long indexing your library would take (from the number of photos in your index) and the space the
  vectors need.

It uses generated test pictures only, never your photos. Options: `--gpu` / `--cpu`, `--runs`, `--batch`,
`--no-cpu-compare`, `--model-root`. Multilingual XLM-Roberta and NLLB models are not supported yet. In the menu this is option **6**: it shows the list, asks which model to check, and asks before downloading (`clipcheck` exits with code 3 when the model is not downloaded).

On an RTX 2060 (6 GB), the default `ViT-L-14-quickgelu__dfn2b` took about 66 ms per photo and `ViT-B-16-SigLIP2__webli`
about 21 ms; turning a search into a vector took about 10 ms with either.

#### Store search vectors for your photos

`embed` runs every indexed photo once through the picture half of a downloaded model and stores the result in the
index (768 numbers per photo for both suggested models, as 16-bit numbers: about 1.5 KB per photo). Like `faces`, it
is a preview until you add `--apply`, saves after every batch, and continues where it stopped:

    python sort_photos.py embed                                  # preview: how many photos would be processed
    python sort_photos.py embed --apply --limit 50               # try it on 50 photos first
    python sort_photos.py embed --apply                          # every photo without a vector for this model
    python sort_photos.py embed --apply --model ViT-B-16-SigLIP2__webli   # a second model, stored side by side

The model must be downloaded first (`clipcheck --model NAME --download`). Each vector is stored with its model's name,
so several models can be compared on the same photos. Photos that are missing or cannot be read are skipped and tried
again next time. Options: `--again` (replace this model's vectors), `--gpu` / `--cpu`, `--batch`, `--workers`,
`--model-root`. New photos added by `sort`/`refresh` get their vector the next time you run `embed --apply`. In the menu this is option **5**, which also offers to download the model when it is missing (`embed` exits with code 3 in that case).

#### Search in the web interface

On the **Photos** page, type into **Describe a photo** and press Enter. Photos come back best match first (hover over one
to see its match score). Everything else on the page still applies, so "birthday cake" + Alice + 2019 works, and so does
combining it with the name/folder/note box. While a description is active:

- **Best 50 / 100 / 200 / 500 / 1000** sets how far down the list to go. There is no fixed "match" cut-off, because each
  model scores on its own scale (the default model's good matches sit around 0.25 to 0.33, SigLIP 2's around 0.10 to 0.18).
- If more than one model has vectors stored, a menu lets you choose which one to search with, so you can compare them.
- A note says how many of the photos on screen have no search vector yet (run option 5 to add them).
- **Clear filters** goes back to the normal newest-first view.

The box is greyed out until some photos have search vectors. The first search after starting the interface loads the
text half of the model (a few seconds); after that a search takes about 0.1 s. Searching runs on the CPU, so the GPU
stays free for face scanning and `embed`.

### Phase 7.5: the **Tasks** page (sort, find faces and prepare search without the menu)

Everything the menu does for new photos can now be done from the web interface. Open **Tasks** in the left column
(it opens there by itself while the index is still empty, so you can start with `python sort_photos.py ui` alone).

- **Photo library** (at the top). The folder your sorted photos live in. It shows how many of its photos are in the
  index, scanned for faces and searchable. **Change…** (or **Choose…** the first time) picks another folder with the
  folder browser; a folder that doesn't exist yet is fine if you are about to sort photos into it. Then **Add this
  library** brings everything up to date in one go: it adds the photos already in that folder to the index (with
  their camera details), finds the faces, groups them and stores search vectors. Untick **Find faces and group people**
  or **Prepare search** to leave those out; **Preview** shows what would be added without changing anything. Run it
  again whenever photos were added, moved or deleted outside this program; anything already done is skipped.
  - If the new folder holds photos the index already knows from the old folder (for example a copy of your library on
    a new drive), those photos are moved over to the new folder in the index and keep their faces, names, notes and
    search vectors. Nothing is copied or deleted on disk.
  - Photos of the old library that aren't in the new one stay in the index and on the Photos page, and the card says
    how many there are. Changing the library never removes anything.
- **Sort new photos.** Choose the folder with the new photos, either by typing or pasting a path or with **Browse…**,
  which lists the folders on this computer (with shortcuts to Home, Pictures, your library and every drive). They go
  into the photo library. Choose **Copy** (originals stay) or **Move** (each original is deleted only after its copy is
  checked), then **Preview**. Nothing changes until you press **Copy these photos** / **Move these photos** under the
  preview.
- **Find faces and group people.** Refreshes the index from the photo library, scans the photos that haven't been
  scanned, then groups the faces, the same three steps as menu option 3. Optionally limit how many photos are scanned
  (for a quick test) and choose GPU or CPU. **Group faces only** runs just the last step.
- **Prepare search.** Choose a search model (the list says which are downloaded and how big the others are), then
  **Preview** or **Process photos**. A model that isn't downloaded yet is fetched once after you agree, then checked.

While a task runs, its messages appear live on the page, each step is ticked off, and the left column shows
**running**. You can go to other pages meanwhile. **Stop** works like Ctrl+C: the photo in progress is finished, all
work so far is kept, and running the task again continues where it stopped. Only one task runs at a time, because they
all write to the same index. When a task finishes, the page offers the obvious next step (for example **Find faces in
them** after sorting, **Name the people** after grouping).

Each task runs the same `sort_photos.py` command you would type, against the index the interface is using (adding a
library is `refresh LIBRARY --apply --adopt`, then `faces`, `cluster` and `embed`), and the library, folders and search
model you choose are remembered in `.photo-sorter.conf` (shared with the menu). Missing Python
packages are pointed out on the page, with the `pip install` command to run. Stopping the web interface also stops a
task that is still running.

### The whole workflow

    python sort_photos.py sort SOURCE DEST --apply     # or: refresh DEST --apply for a library that is already sorted
    python sort_photos.py faces --apply                # find faces (use the GPU if you set it up)
    python sort_photos.py cluster --apply              # group them into people
    python sort_photos.py ui                           # name people, review, merge
    # later, after adding photos: sort/refresh, faces --apply, then "Place new faces" in the UI (or cluster --apply)
    # or do all of it from the Tasks page of the web interface (phase 7.5)

## Menu script, tests and deploying

**On Windows, double-click or run the `.bat` files** (`photo-sorter.bat`, `run-tests.bat`, `deploy.bat`) from Command Prompt or PowerShell. They do the same as the `.sh` scripts below, which are for Git Bash, macOS and Linux. Both need Python 3.9 or newer;
set `PHOTO_SORTER_PYTHON` (for example `py -3`) if the right one is not found automatically. Pressing Ctrl+C while the web interface runs stops it; if Windows then asks "Terminate batch job (Y/N)?", answer **Y** to leave or **N** to return to the menu.

**`photo-sorter.bat` keeps itself up to date.** When the folder is a git checkout on the `main` branch, it first
fetches `main` from GitHub and, if there is a newer version, updates to it and starts that one. It lists what changed
and says when a `requirements*.txt` file changed (it does not install packages itself, so versions pinned for an
older PC stay as they are). It only ever fast-forwards: if you changed files or made your own commits, or GitHub
can't be reached, it says so and starts the version you have. Set `PHOTO_SORTER_NO_UPDATE=1` to skip the check. A
folder made with `deploy.bat` or from a zip download is not a git checkout and is never updated this way.

**`photo-sorter.bat` opens the web interface to your network.** Option 1 starts it with `--host 0.0.0.0`, so other
computers can open it after signing in (see Signing in above, and allow the port in the firewall). To keep it to this
computer, set `PHOTO_SORTER_UI_ARGS` yourself, for example `setx PHOTO_SORTER_UI_ARGS "--host 127.0.0.1"`; whatever
you set there is used instead.

| Script | What it does |
|---|---|
| `photo-sorter.bat` / `./photo-sorter.sh` | Menu: **1** start the web interface, **2** intake and sort photos (asks where they are, shows a preview, then asks before changing anything), **3** find faces and group people (refresh the index, scan for faces, group them), **4** read camera details (preview, then asks; can read every photo again), **5** prepare search (asks which model, shows a preview, offers to download the model if it is missing, then stores the search vectors), **6** check a search model (shows the suggested models, checks and times the one you pick, downloads it only after asking; a model that passes becomes the default for 5), **7** reset the web interface password (asks for a new one; every browser is signed out). Also `photo-sorter.bat ui`, `sort`, `faces`, `metadata`, `search`, `clipcheck`, `password`. Your last folders and search model are remembered in `.photo-sorter.conf`. |
| `run-tests.bat` / `./run-tests.sh` | Syntax checks plus all tests (`--all` adds the browser tests, `--cov` shows what is not covered, `--install` installs the test tools first, `-k word` runs matching tests only). Exit code 0 means everything passed. |
| `deploy.bat` / `./deploy.sh` | Runs the tests, then copies the program files to the production folder, keeping dated backups. `--dry-run` shows what would happen, `--rollback` restores the previous version. It never touches the database, thumbnails, `.photo-sorter.conf` or a virtual environment in production. |

**Production location.** It is still a placeholder (`/path/to/production/photo-sorter`) and `deploy.sh` refuses to run until you set it:
`deploy.bat --target D:\where\it\goes`, or copy `deploy.conf.example` to `deploy.conf` and edit `DEPLOY_TARGET`.

The test plan, with what is automated and what to check by hand, is in `docs/TEST_PLAN.md`.

## Roadmap
- Phase 2: InsightFace detection + embeddings stored in SQLite (done)
- Phase 3: clustering, with low-confidence faces flagged for review (done)
- Phase 4: web interface to name people and work the review queue (done)
- Phase 5: browse the whole library with people quick filters, rename and delete with confirmation (done)
- Phase 6: camera details, notes and a search index (done)
- Phase 7: search by typing a description (done)
- Phase 7.5: a Tasks page in the web interface to sort new photos, find faces and prepare search (done)
- Phase 8 (planned): optional AI captions. The research and design are in [ROADMAP.md](ROADMAP.md).
- Ideas not built yet: writing people's names into photo metadata or folders, an undo button
