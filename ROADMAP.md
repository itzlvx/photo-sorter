# Photo sorter: roadmap and research notes

Status as of 2026-11-16. Phases 1 to 7.5 are built (sort, faces, grouping, web UI to name people, browsing, metadata,
search by description, and running all of it from the web UI). Phase 8 is optional and not started.

| Phase | What | State |
|---|---|---|
| 5 | Browse the whole library in the UI, with quick filters for recognised people | Built |
| 6 | A general place for photo metadata that is not about faces | Built |
| 7 | Search by typing a description ("birthday cake", "dog on a beach") | Built |
| 7.5 | A Tasks page in the web UI: choose folders, sort new photos, find faces, group, prepare search | Built |
| 8 | Optional: written captions from a local vision-language model | Planned, optional, heavy |

## Requirement as understood

Later, you type a description of a photo and the app finds matching photos. That needs the app to understand
what is *in* each photo, which is why it needs a classifier and extra metadata beyond faces. If you also want to
type your own notes onto a photo ("Grandma's 80th, Lake Placid"), that is a small separate feature (a text column
plus search), listed under Phase 6.

## Phase 5: browse and filter (built)

A Photos view of everything in the index, newest first, with quick filters: a person (or several, matching all or any),
faces not yet labelled, no faces, needs review, not scanned yet, year and month, and a filename/folder search. A photo
opens in a viewer with each face outlined and named, and can be renamed or deleted (both ask for confirmation; delete
moves the file to a `.photo-sorter-trash` folder). Photo thumbnails are made on first view and cached on this
computer next to the index (never on the network drive).

## Phase 6: metadata beyond faces (built)

Built as designed below, in `photo_meta.py`. What was decided while building it:

- `photo_exif` also stores aperture, shutter speed, ISO, focal length and orientation; width/height are of the upright
  photo. A row is written even when a file has no EXIF (or cannot be read, with `error` set), so nothing is read twice.
  New photos are read during `sort`/`refresh` from the same file opening that finds the date. Photos indexed earlier are
  read once by the next `refresh --apply`, or by the new `metadata` command (`--limit`, `--reread`, resumable).
- `photo_annotations` has no fixed list of kinds in the database (the program checks it), so a new kind needs no schema
  change. Your notes are `kind='note', source='you'`, one per photo, edited in the photo viewer.
- `photo_embeddings` stores float32 or float16 vectors (the size tells which); `load_embeddings(model)` returns the ids
  and a matrix ready for a numpy similarity search, which is what phase 7 needs.
- The search index `photo_search` (FTS5) has one row per photo (file path + all annotation text) and is maintained by
  triggers inside the database. The Photos page search matches each word against the path (substring, as before) or
  the annotation text (FTS prefix match, accents ignored). Without FTS5 it falls back to a plain text match.
- Annotations cascade with the photo, like faces, so a photo removed by `refresh` (file gone) loses its notes.

Original design:

Design goal: every later feature (search, captions, OCR, scene tags) adds *rows*, not new columns or tables, so
the schema does not have to change each time.

- `photo_exif`: camera make/model, lens, pixel size, GPS if present (read during `refresh`/`sort`).
- `photo_embeddings (photo_id, model, dim, vector BLOB)`: one row per photo per model (same pattern as face embeddings).
- `photo_annotations (photo_id, kind, text, score, source, created_at)`: kind is `caption`, `tag`, `ocr`, `note`.
  `source` says who wrote it (`model:<name>` or `you`), so human notes are never overwritten by a re-run.
- An SQLite full-text index (FTS5, built into SQLite) over annotations and file names, for fast keyword search.
- Everything is keyed by photo id and cascades when a photo is removed, like faces today.

## Phase 7: search by description (the main research question)

### Step 1 built: `clipcheck` (2026-11-16)

In `clip_search.py`, with the shared GPU helpers moved from `face_scan.py` to `onnx_setup.py`. What was decided:

- **Model format:** the Immich ONNX exports, unchanged (`config.json`, `visual/model.onnx` + `preprocess_cfg.json`,
  `textual/model.onnx` + `tokenizer.json` + `tokenizer_config.json`). Input names, batch size and the token type
  (int32/int64) are read from the model, not assumed. Preprocessing follows `preprocess_cfg.json` (`shortest` = crop,
  `squash`, `longest` = pad). JPEGs are decoded at reduced size (Pillow draft mode), which makes a 12-megapixel photo
  cheap to prepare. SigLIP-family tokenizers get lower-case text without punctuation, like OpenCLIP does.
- **Packages:** only `tokenizers` is new (`requirements-search.txt`). ONNX Runtime is deliberately not listed there, so
  pip never installs the CPU build next to `onnxruntime-gpu`.
- **Models live** in `%LOCALAPPDATA%\photo-sorter\models\<name>`. Download only with `--download`: small files first
  (a wrong name fails fast), each file via `.part` and renamed when complete, free disk space checked, the request
  carries only a generic User-Agent.
- **Suggested models** (`--list`): `ViT-L-14-quickgelu__dfn2b` (default, about 1.7 GB), `ViT-B-16-SigLIP2__webli`
  (about 1.5 GB), `ViT-B-32__openai` (about 0.6 GB). Any immich-app CLIP/SigLIP name works; XLM-Roberta/NLLB models need
  a second text input and are refused with a clear message for now.
- **The check** uses generated pictures only: red/green/blue must match their words, then timings (photo decoding,
  pictures on GPU and CPU, one search) and an estimate for the photos in your index.
- Tested with a tiny generated ONNX model (`tests/test_clip_search.py`).

Real runs on the RTX 2060 Max-Q (6 GB), with generated pictures only:

| Model | Download | Picture on GPU | Picture on CPU | One search | Colour check |
|---|---|---|---|---|---|
| `ViT-L-14-quickgelu__dfn2b` | 1.7 GB | 66 ms | 1,020 ms | 9 ms | 3 of 3 (correct about 0.28, next best about 0.15) |
| `ViT-B-16-SigLIP2__webli` | 1.5 GB | 21 ms | 221 ms | 11 ms | 3 of 3 (correct about 0.15, next best about 0.09) |

Both exports accept one picture per call (fixed batch size 1). Opening a 12-megapixel JPEG at reduced size took about
40 ms of CPU time, which `embed` does in background threads.

### Step 2 built: `embed` (2026-11-16)

In `clip_search.py`. One vector per photo per model in `photo_embeddings`, as 16-bit unit vectors (768 numbers, about
1.5 KB per photo for both suggested models). Decisions:

- **Same shape as `faces`:** a preview unless `--apply`, `--limit`, resumable (photos without a vector for this model
  are the work list), `--again` to replace, Ctrl+C keeps every finished batch. Each batch is written in one transaction.
- **The model is never downloaded here**; `embed` points to `clipcheck --model NAME --download`, so a download always
  goes through the check first.
- **Photos are read and shrunk in background threads** (`--workers`, default 4) while the GPU encodes.
- **Missing files** are skipped with the same "is the drive connected?" advice as `faces`. **Unreadable files** are
  skipped and tried again on the next run (nothing is recorded, so no schema change; the run ends with exit code 1 so
  the problem stays visible). The model failing on 3 batches in a row stops the run.
- **Safety checks:** the model is loaded and tried on a test picture before any photo is read, and if this model's
  vectors already in the index have a different size (changed model files), the run stops and asks for `--again`
  instead of mixing them.
- Vectors for other models are listed and never touched, so two models can be compared side by side.

Checked on a throw-away library of 320 generated pictures (20 of them 12-megapixel): DFN-2B 14 photos/s, SigLIP 2
39 photos/s on the GPU, resumed correctly after `--limit`, and the stored vectors equal the model's output for the
same file (cosine 1.000). Not yet run on your real library.

### Step 3 built: the search box (2026-11-16)

A **Describe a photo** box on the Photos page (`/api/photos?d=...`, plus `/api/search/models`). Decisions:

- **The text half runs on the CPU in the web interface.** Measured on this laptop: about 0.1 s per search for both
  suggested models (the first search loads the model: about 2 s for DFN-2B, 6 s for SigLIP 2). This keeps the GPU's
  6 GB free for face scanning and `embed`, and means the web interface never needs CUDA.
- **Only the text half is loaded** (`ClipModel(..., image=False)`), and each model's vectors are kept in memory as 16-bit
  numbers (about 150 MB per 100,000 photos at 768 numbers), compared 20,000 rows at a time. They are read again only when
  they change in the index (count, newest row id or photo ids differ), so `embed`, deleted photos and `--again` are picked
  up without restarting. The last 8 searches are remembered, so "Show more" does not encode the text again.
- **Ranking first, then filters.** Every photo with a vector is scored; the other filters (people, year/month, quick
  filters, the name/folder/note box) then decide which of them are shown, best first.
- **No fixed threshold.** Scores differ per model, so the person chooses how many to see (best 50 to 1000, default 200).
  The match score shows when hovering over a photo.
- **Several models:** the default is the one with the most stored vectors; a menu switches between them.
- **Clear messages instead of errors** for: no vectors yet (points to option 5), model files gone (points to
  `clipcheck --download`), missing packages, changed model (points to `embed --again`).

Checked with the real models on 7 drawn scenes (grass under a sky, a stop sign, a checkerboard, a sunset, the moon, a
birthday cake, a "HAPPY BIRTHDAY" card): both models put the right one first for all 7 descriptions. That shows the
whole path works; it says nothing yet about quality on real family photos.

Still open: try both models on a few hundred of your own photos and keep the better one; multilingual models (XLM-Roberta,
NLLB) need a second text input and are refused for now.

### How it works

CLIP-style models turn an image and a sentence into vectors in the same space; a photo matches a query when the
vectors point the same way. Each photo is processed once (one pass through the image half of the model) and the
vector is stored. At search time only the typed text is encoded, then compared with the stored vectors. This is how
Immich's "smart search" works ([Immich docs](https://docs.immich.app/features/smart-search)), and it finds photos by
content without any keywords in metadata.

### Model choices

| Option | Notes |
|---|---|
| OpenCLIP ViT-L-14 / DFN-2B | PhotoMapAI recommends it as the default: about 82% ImageNet zero-shot against about 63% for legacy CLIP, handles messy mixed photos, about 600 MB, slower to index. Its similarity scores sit in a stable range (matches around 0.20 to 0.35). ([PhotoMapAI](https://lstein.github.io/PhotoMapAI/user-guide/encoders/)) |
| SigLIP 2 | Multilingual. Immich notes its `siglip2` and `xlm` models work regardless of the search language, whereas the `nllb` models need the query in the language set in the settings. PhotoMapAI found it strong on clean single-subject images but weaker on cluttered photos and bare-noun queries, and it needs much lower score thresholds (default 0.005) and more tuning. A SigLIP 2 ViT-B/16 checkpoint on Hugging Face is Apache-2.0 ([model card](https://huggingface.co/MyAlbum/ViT-B-16-SigLIP2)). |
| Legacy OpenAI CLIP ViT-B/32 | Smallest (about 150 MB) and fastest to index, but more false positives on real photos. |

Immich publishes a benchmark table ordering these models by quality and speed per language, and lets you switch
models later by re-processing. Plan for the same: store the model name with every vector and re-index when switching.

### Runtime: the decision that matters most for you

You already run `onnxruntime-gpu` for face scanning. Two ways to run the search model:

1. **ONNX models through the same onnxruntime-gpu (recommended first).** The `immich-app` organisation on Hugging Face
   publishes ONNX exports of OpenCLIP models intended for Immich
   ([example](https://huggingface.co/immich-app/ViT-B-16-SigLIP__webli)). No PyTorch, no second CUDA stack, same GPU
   setup and `gpucheck` as today.
2. **PyTorch + `open_clip`.** The widest model choice and easiest for new models, but a multi-gigabyte install, and
   ONNX Runtime documents that it only coexists with PyTorch when both use the same major CUDA and cuDNN versions,
   with the DLLs preloaded before a session is created
   ([ONNX Runtime CUDA docs](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)). Doable, but
   Windows is where this goes wrong.

I haven't tested either on your hardware. The first Phase 7 step is a `clipcheck` command that times the chosen
model on your GPU before anything is indexed, like `gpucheck` does.

Licences: the model cards I read for the immich-app export did not state one, and the underlying OpenCLIP weights
differ per model. For a personal library that is fine; check the specific model before using it commercially.

### Storing and searching the vectors

Keep the vectors as BLOBs in SQLite and compare with numpy, exactly as faces are handled now. At the scale of a family
library (tens to a few hundred thousand photos, roughly 150 MB for 100,000 photos at 768 numbers stored as 16-bit),
one matrix multiplication per query is fast enough, and it avoids installing a native extension. That sizing is my
own estimate, not a benchmark. If the library ever outgrows it, `sqlite-vec` is the obvious next step; it exists and
is pre-1.0 (version 0.1.x as of a 2026 test I found), which is one more reason not to depend on it yet.

### How search would behave in the UI

A search box on the Photos page. Results are ranked by similarity and can be combined with the filters from Phase 5
("birthday cake" + person Alice + 2019). Because score ranges differ by model, the UI shows "best matches first" with
a cut-off you can adjust, rather than a fixed threshold.

## Phase 7.5: run the tasks from the web interface (built 2026-11-16)

Until now the web interface could only work with what was already in the index; adding photos, finding faces and
storing search vectors needed the menu or the command line. The **Tasks** page closes that gap.

- **Same commands, run in the background.** `ui_tasks.py` starts `sort_photos.py sort / refresh / faces / cluster /
  clipcheck / embed` as separate processes with `--db` set to the index the interface uses, and keeps their newest
  4,000 output lines for the page, which polls for new lines every 0.8 s. Running the real commands (not calling them
  inside the server) means one code path, the server stays responsive, and a crash in a model can't take it down.
- **One task at a time,** because they all write to the index. "Place new faces" refuses to run while a task does,
  and the other way round. After each task, cached people data is refreshed.
- **Stop = Ctrl+C.** On Windows each task gets its own process group and receives Ctrl+Break, which `sort_photos.py`
  now treats like Ctrl+C; elsewhere it gets SIGINT. Every command already saved its progress on Ctrl+C except `sort`,
  which now stops after the current photo, prints its summary and exits with 130, and never leaves a half-copied file
  behind (a copy cut short is deleted; with `--move` the original is only deleted after the copy is checked). If a
  command doesn't stop within 20 s it is ended.
- **Folder browser** lists subfolders (hidden and system folders left out) with shortcuts to Home, Pictures, the
  remembered library and source, and every drive letter (read without touching disconnected network drives). A path
  can always be typed or pasted, quotes from Explorer's "Copy as path" are removed, and relative paths are refused.
- **Same flow as the menu:** sorting is preview first, then apply; faces is refresh, scan, group (a step with some
  problems doesn't stop the next one, a failure does); search downloads the model only after a confirmation that
  shows its size. Folders and the search model are remembered in `.photo-sorter.conf`, shared with the menu.
- **Starting fresh:** `ui` no longer refuses to start without an index. It creates an empty one and opens on Tasks.
- **The photo library as a setting** (added 2026-11-18). The library folder (`LIBRARY_DIR`, shared with the menu) is
  shown at the top of the Tasks page with how many of its photos are indexed, face-scanned and searchable; sorting and
  face scanning use it. **Add this library** runs `refresh --apply --adopt`, `faces`, `cluster` and `embed` in one job.
  Faces and search don't depend on each other, so a problem in one doesn't stop the other; a failed index update
  stops everything. `refresh --adopt` is new: a photo the index has in a folder outside this library (an earlier
  library, still on disk) is re-pointed to its copy here, keeping its row and so its faces, names, notes and vectors.
  Without `--adopt` such copies are still reported as extra copies and left alone. Photos only in the old library stay
  in the index, counted on the card. `cluster` now exits 0 with "nothing to group" when photos were scanned and none had
  a face (it still exits 2 when nothing was scanned yet).

Not done: installing missing Python packages from the page (it shows the `pip` command instead), making a new folder
inside the folder browser (type the path; sorting creates it), and removing an old library's photos from the index
(they stay, counted on the library card; a "forget photos outside the library" button would be the follow-up).

## Phase 8: captions from a vision-language model (optional)

A vision-language model writes a sentence per photo, stored as an annotation and searchable by keyword. This is slower
and heavier than Phase 7, and mainly adds readable text you can see and edit. PhotoPrism benchmarked local models on an
RTX 4060 (8 GB) with 16 photos at 720 px ([PhotoPrism](https://docs.photoprism.app/developer-guide/vision/model-comparison/)):

- Caption speed: MiniCPM-V 4.6 1B 0.6 s and Gemma 4 e2b 0.7 s were fastest; a Qwen3-VL reasoning build took 6.4 s.
- Labels: Qwen3.5 4B had the best coverage (88%); Qwen3-VL 4B reached 97% when asked for an explicit number of labels.
- Accuracy warning: Gemma 4 consistently labelled a cheetah as a leopard and penguins as seals. Coverage numbers cannot
  catch confidently wrong answers. "Bigger is not reliably better."
- It was a single small run, so measure on your own photos before choosing.

My arithmetic from those timings: roughly 2 to 7 hours per 10,000 photos on a mid-range GPU. Phase 7 is far cheaper
(one forward pass per photo), and likely covers most "find the photo of..." needs on its own, so Phase 8 is worth doing
only if you want captions you can read.

Other tagging models (for example Recognize Anything) came up in my search but I did not evaluate them. CLIP can also
produce tags by comparing each photo with a list of words, which avoids another model.

## Suggested order

1. Phase 5 (done): immediately useful, and the filters are reused by search.
2. Phase 6 (done): small, and unblocks everything else.
3. Phase 7 (done) with ONNX CLIP-family models: biggest payoff. What is left is choosing between DFN-2B and SigLIP 2
   by trying both on a few hundred of your photos (both can be stored side by side).
4. Phase 7.5 (done): the whole workflow from the web interface.
5. Phase 8 only if wanted.

## Sources

- [Immich: smart search](https://docs.immich.app/features/smart-search) and [searching](https://docs.immich.app/features/searching)
- [PhotoMapAI: encoders](https://lstein.github.io/PhotoMapAI/user-guide/encoders/)
- [PhotoPrism: vision model comparison](https://docs.photoprism.app/developer-guide/vision/model-comparison/)
- [Roboflow: local vision-language models](https://roboflow.com/blog/local-vision-language-models)
- [SigLIP 2 ViT-B/16 model card](https://huggingface.co/MyAlbum/ViT-B-16-SigLIP2) and [paper](https://arxiv.org/abs/2502.14786)
- [immich-app ONNX CLIP exports](https://huggingface.co/immich-app/ViT-B-16-SigLIP__webli)
- [ONNX Runtime CUDA execution provider](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)
