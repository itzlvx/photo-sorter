"""Phase 7, step 1: the clipcheck command and the model wrapper behind it.

No real model is downloaded. A tiny fake CLIP model is built with the `onnx` package: its "picture vector" is the
average colour, and its "sentence vector" is the sum of fixed word vectors, so red / green / blue pictures match the
sentences with those words. Downloads are served from a local folder through a fake urlopen.
"""
import io
import json
import shutil
import sqlite3
import urllib.error
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")
pytest.importorskip("tokenizers")

import clip_search  # noqa: E402
import onnx_setup  # noqa: E402

from clip_fakes import CLIP_MEAN, CLIP_STD, LENGTH, VOCAB, WORDS, make_model  # noqa: E402,F401


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(clip_search, "CAMERA_PHOTO", (400, 300))  # time a small JPEG instead of 12 megapixels
    monkeypatch.setattr(onnx_setup, "nvidia_gpu_summary", lambda: None)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "models"


@pytest.fixture
def model(root):
    return make_model(root / "test-model")


def check(cli, root, *extra, name="test-model"):
    return cli("clipcheck", "--model", name, "--model-root", root, "--cpu", "--runs", "1", "--batch", "2", *extra)


def cpu_model(folder, text=True):
    return clip_search.ClipModel(folder, ["CPUExecutionProvider"], text=text)


# --------------------------------------------------------------------------- the command
def test_clipcheck_passes_with_a_working_model(cli, root, model):
    r = check(cli, root)
    assert r.code == 0, r.text
    assert "Picture/word check: 3 of 3 right" in r.text
    for name in ("red", "green", "blue"):
        assert f"{name:<5} picture -> best match '{name}'" in r.text
    assert "Pictures run on: CPU; text runs on: CPU" in r.text
    assert "ms per picture" in r.text and "Turning a search into a vector" in r.text
    assert "For 10,000 photos: about" in r.text and "16-bit" in r.text
    assert "RESULT: test-model works on CPU" in r.text


def test_estimate_uses_the_number_of_photos_in_the_index(cli, root, model, env):
    env.db.parent.mkdir(parents=True)
    conn = sqlite3.connect(env.db)
    conn.execute("CREATE TABLE photos (id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO photos VALUES (?)", [(i,) for i in range(1, 1235)])
    conn.commit()
    conn.close()
    r = check(cli, root)
    assert r.code == 0 and "Your index has 1,234 photos: about" in r.text


def test_a_mismatched_model_fails_the_picture_word_check(cli, root):
    make_model(root / "swapped", words={"red": [0, 1, 0], "green": [1, 0, 0], "blue": [0, 0, 1]})
    r = check(cli, root, name="swapped")
    assert r.code == 1 and "only 1 of 3 right" in r.text and "<-- wrong" in r.text and "did not pass" in r.text


def test_missing_model_explains_how_to_download_it(cli, root):
    r = cli("clipcheck", "--model-root", root, "--cpu")
    assert r.code == 3  # its own exit code, so the menu can offer the download
    assert "not downloaded yet" in r.text and "1.7 GB" in r.text
    assert f"clipcheck --model {clip_search.DEFAULT_MODEL} --download" in r.text


@pytest.mark.parametrize("name", ["../evil", "a/b", "..", "", "x" * 101, "bad name"])
def test_model_names_that_could_escape_the_folder_or_url_are_refused(cli, root, name):
    r = cli("clipcheck", "--model", name, "--model-root", root)
    assert r.code == 2 and "not a valid model name" in r.text


def test_missing_packages_are_explained(cli, root, model, monkeypatch):
    monkeypatch.setattr(clip_search, "runtime_problem", lambda: "ImportError: no tokenizers. Install it with: pip install -r requirements-search.txt")
    r = check(cli, root)
    assert r.code == 2 and "requirements-search.txt" in r.text


def test_gpu_required_but_not_available(cli, root, model, monkeypatch):
    def refuse(mode="auto"):
        raise RuntimeError("--gpu was given, but the installed ONNX Runtime has no NVIDIA CUDA support")
    monkeypatch.setattr(onnx_setup, "pick_providers", refuse)
    r = cli("clipcheck", "--model", "test-model", "--model-root", root, "--gpu")
    assert r.code == 1 and "--gpu was given" in r.text


def test_damaged_model_files_are_reported(cli, root, model):
    (model / "config.json").write_text("{not json", encoding="utf-8")
    r = check(cli, root)
    assert r.code == 1 and "cannot be used" in r.text and "config.json" in r.text
    make_model(model)
    (model / "visual" / "model.onnx").write_bytes(b"not a model")
    r = check(cli, root)
    assert r.code == 1 and "could not be loaded" in r.text


def test_list_shows_suggestions_and_what_is_downloaded(cli, root, model):
    make_model(root / clip_search.DEFAULT_MODEL)
    (root / "half-done").mkdir()
    r = cli("clipcheck", "--list", "--model-root", root)
    assert r.code == 0
    lines = r.text.splitlines()
    assert any(clip_search.DEFAULT_MODEL in l and "(downloaded, default)" in l for l in lines)
    assert any("ViT-B-32__openai" in l and "not downloaded" in l for l in lines)
    assert any("test-model" in l and "(complete)" in l for l in lines)
    assert any("half-done" in l and "(incomplete)" in l for l in lines)


def test_default_models_folder_is_next_to_the_index(tmp_path):
    assert clip_search.model_folder(None, "m") == tmp_path / "localappdata" / "photo-sorter" / "models" / "m"


# --------------------------------------------------------------------------- downloading
class FakeHub:
    """Serves a model folder the way huggingface.co/immich-app/<name>/resolve/main/<file> would."""

    def __init__(self, source: Path, name: str):
        self.source, self.name = source, name
        self.requests, self.fail = [], {}

    def urlopen(self, request, timeout=None):
        url = request.full_url
        self.requests.append((url, request.get_header("User-agent")))
        prefix = f"{clip_search.HF_BASE}/{self.name}/resolve/main/"
        if not url.startswith(prefix) or not (self.source / url[len(prefix):]).is_file():
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        rel = url[len(prefix):]
        data = (self.source / rel).read_bytes()
        how = self.fail.get(rel)
        if how == "offline":
            raise urllib.error.URLError("no route to host")
        if how == "interrupt":
            raise KeyboardInterrupt
        length = len(data) + (10 if how == "short" else 0)
        return Response(data, length)


class Response(io.BytesIO):
    def __init__(self, data, length):
        super().__init__(data)
        self.headers = {"Content-Length": str(length)}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    source = make_model(tmp_path / "hub" / "remote-model")
    fake = FakeHub(source, "remote-model")
    monkeypatch.setattr(clip_search.urllib.request, "urlopen", fake.urlopen)
    return fake


def test_download_fetches_every_file_then_checks_the_model(cli, root, hub):
    r = check(cli, root, "--download", name="remote-model")
    assert r.code == 0, r.text
    assert "Download complete." in r.text and "RESULT: remote-model works" in r.text
    fetched = [url.rsplit("/main/", 1)[1] for url, _ in hub.requests]
    assert fetched == list(clip_search.MODEL_FILES)
    assert all(agent == "photo-sorter" for _, agent in hub.requests)   # nothing personal is sent
    assert not list(root.rglob("*.part"))
    hub.requests.clear()
    assert check(cli, root, "--download", name="remote-model").code == 0 and hub.requests == []   # already here


def test_download_only_fetches_what_is_missing(cli, root, hub):
    folder = root / "remote-model"
    shutil.copytree(hub.source, folder)
    (folder / "visual" / "model.onnx").unlink()
    assert check(cli, root, "--download", name="remote-model").code == 0
    assert [url.rsplit("/main/", 1)[1] for url, _ in hub.requests] == ["visual/model.onnx"]


def test_download_of_an_unknown_model_says_so(cli, root, hub):
    r = check(cli, root, "--download", name="No-Such-Model")
    assert r.code == 1 and "was not found on Hugging Face" in r.text and "404" in r.text
    assert not list(root.rglob("*.*"))   # the first (small) file failed, nothing was written


@pytest.mark.parametrize("how, code, text", [
    ("offline", 1, "Check the internet connection"),
    ("short", 1, "stopped early"),
    ("interrupt", 130, "Run the same command again"),
])
def test_a_failed_download_leaves_no_half_file_and_can_be_resumed(cli, root, hub, how, code, text):
    hub.fail["visual/model.onnx"] = how
    r = check(cli, root, "--download", name="remote-model")
    assert r.code == code and text in r.text
    folder = root / "remote-model"
    assert not (folder / "visual" / "model.onnx").exists() and not list(root.rglob("*.part"))
    assert (folder / "textual" / "model.onnx").is_file()        # what finished is kept
    hub.fail.clear()
    hub.requests.clear()
    assert check(cli, root, "--download", name="remote-model").code == 0
    assert [url.rsplit("/main/", 1)[1] for url, _ in hub.requests] == ["visual/model.onnx"]


def test_download_refuses_when_the_disk_is_full(cli, root, hub, monkeypatch):
    monkeypatch.setattr(clip_search.shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10, 10, 0))
    r = check(cli, root, "--download", name="remote-model")
    assert r.code == 1 and "Not enough free disk space" in r.text


# --------------------------------------------------------------------------- the model wrapper
def picture(w, h, left=(255, 0, 0), right=(0, 0, 255)):
    image = Image.new("RGB", (w, h), left)
    image.paste(Image.new("RGB", (w - w // 2, h), right), (w // 2, 0))
    return image


def undo(model, array):
    """Prepared array -> 0-255 RGB, (H, W, 3)."""
    return np.round((array.transpose(1, 2, 0) * model.std + model.mean) * 255)


def test_prepare_shortest_crops_the_long_side_evenly(root):
    model = cpu_model(make_model(root / "m", size=8))
    out = undo(model, model.prepare(picture(400, 200)))
    assert out.shape == (8, 8, 3)
    assert list(out[4, 0]) == [255, 0, 0] and list(out[4, 7]) == [0, 0, 255]


def test_prepare_squash_keeps_everything(root):
    model = cpu_model(make_model(root / "m", size=8, resize_mode="squash"))
    out = undo(model, model.prepare(picture(400, 200)))
    assert out.shape == (8, 8, 3) and list(out[0, 0]) == [255, 0, 0] and list(out[7, 7]) == [0, 0, 255]


def test_prepare_longest_pads_with_the_fill_colour(root):
    model = cpu_model(make_model(root / "m", size=8, resize_mode="longest"))
    out = undo(model, model.prepare(picture(400, 200, left=(255, 255, 255), right=(255, 255, 255))))
    assert list(out[0, 4]) == [0, 0, 0] and list(out[4, 4]) == [255, 255, 255] and list(out[7, 4]) == [0, 0, 0]


def test_prepare_turns_photos_upright_and_decodes_big_jpegs_small(root):
    model = cpu_model(make_model(root / "m", size=8, resize_mode="longest"))
    exif = Image.Exif()
    exif[0x0112] = 6                                     # "rotate 90° clockwise to view"
    data = io.BytesIO()
    picture(400, 200, (255, 255, 255), (255, 255, 255)).save(data, "JPEG", exif=exif)
    out = undo(model, model.prepare(Image.open(io.BytesIO(data.getvalue()))))
    assert np.all(out[4, 0] < 40) and np.all(out[4, 4] > 215)   # upright it is tall: padded left and right
    big = io.BytesIO()
    Image.new("RGB", (4000, 3000), (10, 200, 30)).save(big, "JPEG")
    image = Image.open(io.BytesIO(big.getvalue()))
    assert model.prepare(image).shape == (3, 8, 8) and image.size[0] < 4000   # draft mode decoded it reduced


def test_encode_images_handles_fixed_batch_models_and_returns_unit_vectors(root):
    model = cpu_model(make_model(root / "m", batch=1))
    assert model.fixed_batch == 1
    pictures = np.stack([model.prepare(Image.new("RGB", (16, 16), (i * 40, 100, 50))) for i in range(5)])
    vectors = model.encode_images(pictures)
    assert vectors.shape == (5, 3) and np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)


def test_tokens_are_padded_and_cut_to_the_context_length(root):
    model = cpu_model(make_model(root / "m"))
    assert model.tokens("a red picture").tolist() == [[2, 5, 4, 0, 0, 0]]
    assert model.tokens("a red red red red red red red").shape == (1, LENGTH)
    assert model.tokens("a red picture").dtype == np.int32
    assert not model.canonicalize


def test_siglip_style_tokenizers_get_lower_case_text_without_punctuation(root):
    model = cpu_model(make_model(root / "m", tokenizer_class="GemmaTokenizerFast"))
    assert model.canonicalize and model.tokens("A RED, picture!").tolist() == [[2, 5, 4, 0, 0, 0]]


@pytest.mark.parametrize("text, canon, expected", [
    ("  a   dog\n on a beach ", False, "a dog on a beach"),
    ("Dog_on a BEACH!!", True, "dog on a beach"),
    ("Grandma's 80th", False, "Grandma's 80th"),
    ("Grandma's 80th", True, "grandmas 80th"),
])
def test_clean_text(text, canon, expected):
    assert clip_search.clean_text(text, canon) == expected


def test_text_models_with_extra_inputs_are_refused_clearly(root):
    with pytest.raises(clip_search.ModelError, match="not supported yet"):
        cpu_model(make_model(root / "m", extra_input=True))


def test_image_only_loading_skips_the_text_half(root):
    folder = make_model(root / "m")
    (folder / "textual" / "model.onnx").write_bytes(b"broken")
    model = cpu_model(folder, text=False)
    assert model.textual is None and model.encode_images(np.zeros((1, 3, 8, 8), dtype=np.float32)).shape == (1, 3)


def test_unit_rows_leaves_zero_vectors_alone():
    out = clip_search.unit_rows(np.array([[3, 4], [0, 0]]))
    assert np.allclose(out, [[0.6, 0.8], [0, 0]])


@pytest.mark.parametrize("seconds, text", [(30, "30 seconds"), (600, "10 minutes"), (7200, "2.0 hours")])
def test_human_time(seconds, text):
    assert clip_search.human_time(seconds) == text


# --------------------------------------------------------------------------- the embed command
SOLID = {"red": (230, 10, 10), "green": (10, 200, 30), "blue": (10, 30, 230), "grey": (128, 128, 128), "black": (12, 12, 12)}


def library(env, cli, colours=tuple(SOLID)):
    """Sort one solid-colour photo per colour into the library and index it; returns {colour: photo id}."""
    for name in colours:
        Image.new("RGB", (64, 48), SOLID[name]).save(env.src / f"{name}.jpg", "JPEG", quality=95)
    assert cli("sort", env.src, env.dest, "--apply").code == 0
    conn = sqlite3.connect(env.db)
    try:
        return {Path(path).stem: pid for pid, path in conn.execute("SELECT id, sorted_path FROM photos")}
    finally:
        conn.close()


def embed(cli, root, *extra, name="test-model"):
    return cli("embed", "--model", name, "--model-root", root, "--cpu", "--workers", "2", *extra)


def vectors(db, model="test-model"):
    conn = sqlite3.connect(db)
    try:
        return {pid: (dim, blob) for pid, dim, blob in
                conn.execute("SELECT photo_id, dim, vector FROM photo_embeddings WHERE model = ?", (model,))}
    finally:
        conn.close()


def test_embed_preview_counts_and_writes_nothing(cli, env, root, model):
    library(env, cli)
    r = embed(cli, root)
    assert r.code == 0, r.text
    assert "dry run" in r.text and "To process now: 5" in r.text and "Re-run with --apply" in r.text
    assert vectors(env.db) == {}


def test_embed_stores_a_16_bit_unit_vector_per_photo_that_matches_its_content(cli, env, root, model):
    import photo_meta
    ids = library(env, cli)
    r = embed(cli, root, "--apply", "--batch", "2")
    assert r.code == 0, r.text
    assert "Photos processed:     5 of 5" in r.text and "5 of 5 photos have a test-model vector" in r.text
    stored = vectors(env.db)
    assert set(stored) == set(ids.values())
    assert all(dim == 3 and len(blob) == 3 * 2 for dim, blob in stored.values())  # 16-bit numbers

    conn = sqlite3.connect(env.db)
    found, matrix = photo_meta.load_embeddings(conn, "test-model")
    conn.close()
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-2)
    text = cpu_model(model).encode_texts(["red", "green", "blue"])
    rgb = [found.index(ids[c]) for c in ("red", "green", "blue")]
    for word, colour in enumerate(("red", "green", "blue")):
        scores = matrix[rgb] @ text[word]
        assert rgb[int(np.argmax(scores))] == found.index(ids[colour])  # the best match for the word is that photo

    again = embed(cli, root, "--apply")
    assert again.code == 0 and "To process now: 0" in again.text and "Nothing to process" in again.text


def test_embed_limit_then_resume(cli, env, root, model):
    library(env, cli)
    r = embed(cli, root, "--apply", "--limit", "2")
    assert r.code == 0 and "limited from 5" in r.text and "3 more photos still to process" in r.text
    assert len(vectors(env.db)) == 2
    r = embed(cli, root, "--apply")
    assert r.code == 0 and "with a test-model vector already: 2" in r.text and "To process now: 3" in r.text
    assert len(vectors(env.db)) == 5


def test_embed_again_replaces_vectors_and_other_models_are_left_alone(cli, env, root, model):
    import photo_meta
    ids = library(env, cli)
    conn = sqlite3.connect(env.db)
    photo_meta.save_embedding(conn, ids["red"], "other-model", [1, 2, 3, 4])
    conn.commit()
    conn.close()
    assert embed(cli, root, "--apply").code == 0
    first = vectors(env.db)
    r = embed(cli, root, "--apply", "--again")
    assert r.code == 0 and "To process now: 5" in r.text
    assert "also stored: 1 vectors from other-model, 4 numbers each" in r.text
    assert {k: v[1] for k, v in vectors(env.db).items()} == {k: v[1] for k, v in first.items()}
    assert len(vectors(env.db, "other-model")) == 1


def test_embed_works_with_models_that_take_one_picture_at_a_time(cli, env, root):
    make_model(root / "single", batch=1)
    library(env, cli)
    r = embed(cli, root, "--apply", "--batch", "4", name="single")
    assert r.code == 0 and "one at a time" in r.text and len(vectors(env.db, "single")) == 5


def test_embed_needs_an_index_and_a_downloaded_model(cli, env, root, model):
    r = embed(cli, root, "--apply")
    assert r.code == 2 and "There is no index yet" in r.text
    library(env, cli)
    r = embed(cli, root, "--apply", name="not-downloaded")
    assert r.code == 3 and "not downloaded yet" in r.text  # its own exit code, so the menu can offer the download
    assert "clipcheck --model not-downloaded --download" in r.text
    preview = embed(cli, root, name=clip_search.DEFAULT_MODEL)
    assert preview.code == 3 and "To process now: 5" in preview.text and "about 1.7 GB" in preview.text


def test_embed_explains_missing_packages_and_refuses_bad_names(cli, env, root, model, monkeypatch):
    library(env, cli)
    assert embed(cli, root, "--apply", name="../evil").code == 2
    monkeypatch.setattr(clip_search, "runtime_problem", lambda: "ImportError: no tokenizers. Install it with: pip install -r requirements-search.txt")
    r = embed(cli, root, "--apply")
    assert r.code == 2 and "requirements-search.txt" in r.text


def test_embed_with_gpu_required_but_not_available(cli, env, root, model, monkeypatch):
    library(env, cli)

    def refuse(mode="auto"):
        raise RuntimeError("--gpu was given, but the installed ONNX Runtime has no NVIDIA CUDA support")
    monkeypatch.setattr(onnx_setup, "pick_providers", refuse)
    r = cli("embed", "--model", "test-model", "--model-root", root, "--gpu", "--apply")
    assert r.code == 2 and "--gpu was given" in r.text and vectors(env.db) == {}


def test_embed_damaged_model_is_reported_before_any_photo(cli, env, root, model):
    library(env, cli)
    (model / "visual" / "model.onnx").write_bytes(b"not a model")
    r = embed(cli, root, "--apply")
    assert r.code == 2 and "cannot be used" in r.text and "clipcheck --model test-model" in r.text


def test_embed_skips_missing_and_unreadable_photos_and_retries_them_later(cli, env, root, model):
    ids = library(env, cli)
    conn = sqlite3.connect(env.db)
    paths = {pid: Path(p) for pid, p in conn.execute("SELECT id, sorted_path FROM photos")}
    conn.close()
    paths[ids["red"]].unlink()
    paths[ids["green"]].write_bytes(b"this is not a photo")
    r = embed(cli, root, "--apply")
    assert r.code == 1, r.text
    assert "File no longer exists, skipped" in r.text and "1 photos no longer exist on disk" in r.text
    assert "Could not read" in r.text and "could not be read:  1 (tried again next time)" in r.text
    assert set(vectors(env.db)) == set(ids.values()) - {ids["red"], ids["green"]}

    Image.new("RGB", (64, 48), SOLID["green"]).save(paths[ids["green"]], "JPEG")  # fixed by hand
    r = embed(cli, root, "--apply")
    assert "To process now: 2" in r.text and ids["green"] in vectors(env.db)


def test_embed_stops_when_the_model_keeps_failing(cli, env, root, model, monkeypatch):
    library(env, cli)
    real = clip_search.ClipModel.encode_images
    calls = {"n": 0}

    def flaky(self, batch):
        calls["n"] += 1
        if calls["n"] == 1:  # the check while loading still works
            return real(self, batch)
        raise RuntimeError("CUDA error: out of memory")
    monkeypatch.setattr(clip_search.ClipModel, "encode_images", flaky)
    r = embed(cli, root, "--apply", "--batch", "1")
    assert r.code == 1
    assert r.text.count("The model failed on 1 photo(s)") == 3
    assert "Stopped early: the model failed on 3 batches in a row" in r.text
    assert vectors(env.db) == {}


def test_embed_ctrl_c_keeps_finished_batches_and_resumes(cli, env, root, model, monkeypatch):
    library(env, cli)
    real = clip_search.save_vectors
    calls = {"n": 0}

    def interrupt(*a):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(*a)
    monkeypatch.setattr(clip_search, "save_vectors", interrupt)
    r = embed(cli, root, "--apply", "--batch", "2")
    assert r.code == 130 and "Stopped by you" in r.text
    assert len(vectors(env.db)) == 2  # the first batch, never half a batch
    monkeypatch.setattr(clip_search, "save_vectors", real)
    r = embed(cli, root, "--apply", "--batch", "2")
    assert r.code == 0 and "To process now: 3" in r.text and len(vectors(env.db)) == 5


def test_embed_refuses_to_mix_vector_sizes_for_one_model(cli, env, root, model):
    import photo_meta
    ids = library(env, cli)
    conn = sqlite3.connect(env.db)
    photo_meta.save_embedding(conn, ids["red"], "test-model", [1, 2, 3, 4, 5], half=True)
    conn.commit()
    conn.close()
    r = embed(cli, root, "--apply")
    assert r.code == 2 and "5 numbers each, but this model makes 3" in r.text and "--again" in r.text
    r = embed(cli, root, "--apply", "--again")
    assert r.code == 0 and all(dim == 3 for dim, _ in vectors(env.db).values())


def test_photo_removed_from_the_index_loses_its_vector(cli, env, root, model):
    ids = library(env, cli)
    assert embed(cli, root, "--apply").code == 0
    conn = sqlite3.connect(env.db)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("DELETE FROM photos WHERE id = ?", (ids["red"],))
    conn.commit()
    conn.close()
    assert ids["red"] not in vectors(env.db) and len(vectors(env.db)) == 4


# --------------------------------------------------------------------------- searching by description (web interface)
SEARCH_COLOURS = ("red", "green", "blue", "black")  # (mid-grey and yellow confuse this colour-only fake model)


@pytest.fixture
def searchable(env, cli):
    """A library of solid-colour photos with vectors from the fake model (kept where the web interface looks for models)."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    import ui_server
    from fastapi.testclient import TestClient

    make_model(clip_search.default_model_root() / "test-model")
    ids = library(env, cli, SEARCH_COLOURS)
    assert cli("embed", "--model", "test-model", "--cpu", "--apply").code == 0
    app = ui_server.create_app(env.db)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    return type("Searchable", (), {"app": app, "client": client, "ids": ids, "db": env.db})()


def found(searchable, **params):
    r = searchable.client.get("/api/photos", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def colour_of(searchable, photo_id):
    return next(name for name, pid in searchable.ids.items() if pid == photo_id)


def test_search_by_description_ranks_the_best_match_first(searchable):
    for word in ("red", "green", "blue"):
        data = found(searchable, d=f"a {word} picture")
        assert colour_of(searchable, data["photos"][0]["id"]) == word
        scores = [p["score"] for p in data["photos"]]
        assert scores == sorted(scores, reverse=True) and len(scores) == 4 and data["total"] == 4
        assert data["search"] == {"model": "test-model", "without_vectors": 0, "top": 200}


def test_search_combines_with_the_other_filters(searchable):
    data = found(searchable, d="red", q="blue")   # the file-name filter still applies
    assert [colour_of(searchable, p["id"]) for p in data["photos"]] == ["blue"]
    data = found(searchable, d="red", year="1999")
    assert data["total"] == 0 and data["photos"] == []


def test_search_paging_and_the_number_of_matches(searchable):
    first = found(searchable, d="green", limit=2)
    rest = found(searchable, d="green", limit=2, offset=2)
    assert len(first["photos"]) == 2 and len(rest["photos"]) == 2 and first["total"] == 4
    assert not {p["id"] for p in first["photos"]} & {p["id"] for p in rest["photos"]}
    assert found(searchable, d="green", top=7)["search"]["top"] == 50    # snapped to an offered choice
    assert found(searchable, d="green", top=100000)["search"]["top"] == 1000


def test_photos_without_a_vector_are_counted_and_new_vectors_are_picked_up(searchable):
    conn = sqlite3.connect(searchable.db)
    conn.execute("DELETE FROM photo_embeddings WHERE photo_id = ?", (searchable.ids["red"],))
    conn.commit()
    conn.close()
    data = found(searchable, d="red")
    assert data["search"]["without_vectors"] == 1 and searchable.ids["red"] not in [p["id"] for p in data["photos"]]
    with sqlite3.connect(searchable.db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM photo_embeddings").fetchone()[0] == 3
    import photo_meta
    conn = sqlite3.connect(searchable.db)
    photo_meta.save_embedding(conn, searchable.ids["red"], "test-model", [1, -1, -1], half=True)
    conn.commit()
    conn.close()
    data = found(searchable, d="red")   # the stored vectors changed, so they are read again
    assert data["photos"][0]["id"] == searchable.ids["red"] and data["search"]["without_vectors"] == 0


def test_a_deleted_photo_disappears_from_the_results(searchable):
    r = searchable.client.post(f"/api/photos/{searchable.ids['red']}/delete", headers={"X-Photo-Sorter": "1"})
    assert r.status_code == 200
    data = found(searchable, d="red")
    assert data["total"] == 3 and searchable.ids["red"] not in [p["id"] for p in data["photos"]]


def test_search_models_lists_what_is_stored_and_usable(searchable):
    import photo_meta
    conn = sqlite3.connect(searchable.db)
    photo_meta.save_embedding(conn, searchable.ids["red"], "gone-model", [1, 2, 3], half=True)
    conn.commit()
    conn.close()
    data = searchable.client.get("/api/search/models").json()
    assert data["default"] == "test-model" and data["limits"] == list(clip_search.SEARCH_LIMITS)
    by_name = {m["name"]: m for m in data["models"]}
    assert by_name["test-model"]["photos"] == 4 and by_name["test-model"]["ready"]
    assert not by_name["gone-model"]["ready"] and "clipcheck --model gone-model --download" in by_name["gone-model"]["problem"]
    r = searchable.client.get("/api/photos", params={"d": "red", "model": "gone-model"})
    assert r.status_code == 409 and "not on this computer" in r.json()["detail"]
    r = searchable.client.get("/api/photos", params={"d": "red", "model": "never-stored"})
    assert r.status_code == 400 and "no search vectors" in r.json()["detail"]


def test_search_explains_when_nothing_is_prepared(env, cli):
    pytest.importorskip("fastapi")
    import ui_server
    from fastapi.testclient import TestClient
    library(env, cli)
    client = TestClient(ui_server.create_app(env.db), base_url="http://127.0.0.1:8765")
    r = client.get("/api/photos", params={"d": "red"})
    assert r.status_code == 409 and "option 5" in r.json()["detail"]
    assert client.get("/api/search/models").json() == {"models": [], "default": "", "limits": list(clip_search.SEARCH_LIMITS), "unavailable": ""}
    assert client.get("/api/photos", params={"d": "   "}).json()["total"] == 5   # a blank description is no search


def test_search_explains_missing_packages_and_mixed_vector_sizes(searchable, monkeypatch):
    import photo_meta
    with monkeypatch.context() as patch:   # (monkeypatch.undo() would also undo the private app-data folder)
        patch.setattr(clip_search, "runtime_problem", lambda: "ImportError: no tokenizers. Install it with: pip install -r requirements-search.txt")
        r = searchable.client.get("/api/photos", params={"d": "red"})
        assert r.status_code == 409 and "requirements-search.txt" in r.json()["detail"]
    conn = sqlite3.connect(searchable.db)
    photo_meta.save_embedding(conn, searchable.ids["red"], "test-model", [1, 2, 3, 4], half=True)
    conn.commit()
    conn.close()
    r = searchable.client.get("/api/photos", params={"d": "red"})
    assert r.status_code == 409 and "embed --apply --again --model test-model" in r.json()["detail"]


def test_the_text_model_is_loaded_once_and_only_its_text_half(searchable, monkeypatch):
    made = []
    real = clip_search.ClipModel.__init__

    def counting(self, *args, **kwargs):
        made.append(kwargs)
        real(self, *args, **kwargs)
    monkeypatch.setattr(clip_search.ClipModel, "__init__", counting)
    for word in ("red", "green", "red", "blue"):
        found(searchable, d=word)
    assert made == [{"text": True, "image": False}]
    engine = searchable.app.state.search
    assert engine.text_model("test-model").visual is None and engine.providers == ["CPUExecutionProvider"]


def test_a_damaged_search_model_is_explained(searchable):
    (clip_search.default_model_root() / "test-model" / "textual" / "model.onnx").write_bytes(b"not a model")
    r = searchable.client.get("/api/photos", params={"d": "red"})
    assert r.status_code == 409 and "cannot be used" in r.json()["detail"]
