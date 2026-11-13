# Photo sorter: roadmap and research notes

Status as of 2026-11-13. Phases 1 to 4 are built (sort, faces, grouping, web UI to name people).

| Phase | What | State |
|---|---|---|
| 5 | Browse the whole library in the UI, with quick filters for recognised people | Built |
| 6 | A general place for photo metadata that is not about faces | Planned |
| 7 | Search by typing a description ("birthday cake", "dog on a beach") | Planned, research below |
| 8 | Optional: written captions from a local vision-language model | Planned, optional, heavy |

## Requirement as understood

Later, you type a description of a photo and the app finds matching photos. That needs the app to understand
what is *in* each photo, which is why it needs a classifier and extra metadata beyond faces. If you also want to
type your own notes onto a photo ("Grandma's 80th, Lake Placid"), that is a small separate feature (a text column
plus search), listed under Phase 6.

## Phase 5: browse and filter (built)

A Photos view of everything in the index, newest first, with quick filters: a person (or several, matching all or any),
faces not yet labelled, no faces, needs review, not scanned yet, year and month, and a filename/folder search. A photo
opens in a viewer with each face outlined and named, and can be renamed or deleted (both ask for confirmation; delete moves the file to a `.photo-sorter-trash` folder). Photo thumbnails are made on first view and cached on this
computer next to the index (never on the network drive).

## Phase 6: metadata beyond faces

Design goal: every later feature (search, captions, OCR, scene tags) adds *rows*, not new columns or tables, so
the schema does not have to change each time.

- `photo_exif`: camera make/model, lens, pixel size, GPS if present (read during `refresh`/`sort`).
- `photo_embeddings (photo_id, model, dim, vector BLOB)`: one row per photo per model (same pattern as face embeddings).
- `photo_annotations (photo_id, kind, text, score, source, created_at)`: kind is `caption`, `tag`, `ocr`, `note`.
  `source` says who wrote it (`model:<name>` or `you`), so human notes are never overwritten by a re-run.
- An SQLite full-text index (FTS5, built into SQLite) over annotations and file names, for fast keyword search.
- Everything is keyed by photo id and cascades when a photo is removed, like faces today.

## Phase 7: search by description (the main research question)

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
2. Phase 6: small, and unblocks everything else.
3. Phase 7 with ONNX CLIP-family model: biggest payoff. Start with `clipcheck`, then pick between the DFN-2B-class
   and SigLIP 2 models by trying both on a few hundred of your photos.
4. Phase 8 only if wanted.

## Sources

- [Immich: smart search](https://docs.immich.app/features/smart-search) and [searching](https://docs.immich.app/features/searching)
- [PhotoMapAI: encoders](https://lstein.github.io/PhotoMapAI/user-guide/encoders/)
- [PhotoPrism: vision model comparison](https://docs.photoprism.app/developer-guide/vision/model-comparison/)
- [Roboflow: local vision-language models](https://roboflow.com/blog/local-vision-language-models)
- [SigLIP 2 ViT-B/16 model card](https://huggingface.co/MyAlbum/ViT-B-16-SigLIP2) and [paper](https://arxiv.org/abs/2502.14786)
- [immich-app ONNX CLIP exports](https://huggingface.co/immich-app/ViT-B-16-SigLIP__webli)
- [ONNX Runtime CUDA execution provider](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)
