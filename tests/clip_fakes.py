"""A tiny fake CLIP model in the Immich ONNX layout, for tests (needs the `onnx` and `tokenizers` packages).

Its "picture vector" is the average colour after normalising, and its "sentence vector" is the sum of fixed word
vectors, so red / green / blue pictures match the sentences with those words.
"""
import json
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from tokenizers import Tokenizer, models, pre_tokenizers

CLIP_MEAN = [0.48145466, 0.4578275, 0.40821073]
CLIP_STD = [0.26862954, 0.26130258, 0.27577711]
VOCAB = {"<pad>": 0, "[UNK]": 1, "a": 2, "plain": 3, "picture": 4, "red": 5, "green": 6, "blue": 7}
WORDS = {"red": [1, 0, 0], "green": [0, 1, 0], "blue": [0, 0, 1]}
LENGTH = 6


def save(model, path: Path):
    model.ir_version = 8
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))


def visual_onnx(path: Path, size=8, batch="batch"):
    weight = numpy_helper.from_array(np.eye(3, dtype=np.float32), "W")
    nodes = [helper.make_node("ReduceMean", ["image"], ["mean"], axes=[2, 3], keepdims=0),
             helper.make_node("MatMul", ["mean", "W"], ["embedding"])]
    graph = helper.make_graph(nodes, "visual", [helper.make_tensor_value_info("image", TensorProto.FLOAT, [batch, 3, size, size])],
                              [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, [batch, 3])], [weight])
    save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)]), path)


def textual_onnx(path: Path, words=WORDS, extra_input=False):
    table = np.zeros((len(VOCAB), 3), dtype=np.float32)
    for word, vector in words.items():
        table[VOCAB[word]] = vector
    inits = [numpy_helper.from_array(table, "table"), numpy_helper.from_array(np.array([1], dtype=np.int64), "axes")]
    nodes = [helper.make_node("Gather", ["table", "text"], ["vectors"], axis=0),
             helper.make_node("ReduceSum", ["vectors", "axes"], ["embedding"], keepdims=0)]
    inputs = [helper.make_tensor_value_info("text", TensorProto.INT32, [1, LENGTH])]
    if extra_input:
        inputs.append(helper.make_tensor_value_info("attention_mask", TensorProto.INT32, [1, LENGTH]))
    graph = helper.make_graph(nodes, "textual", inputs, [helper.make_tensor_value_info("embedding", TensorProto.FLOAT, [1, 3])], inits)
    save(helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)]), path)


def make_model(folder: Path, size=8, resize_mode="shortest", batch="batch", words=WORDS, extra_input=False, tokenizer_class="CLIPTokenizer"):
    (folder / "visual").mkdir(parents=True, exist_ok=True)
    (folder / "textual").mkdir(parents=True, exist_ok=True)
    (folder / "config.json").write_text(json.dumps({"embed_dim": 3, "text_cfg": {"context_length": LENGTH}}), encoding="utf-8")
    (folder / "visual" / "preprocess_cfg.json").write_text(json.dumps({
        "size": [size, size], "mode": "RGB", "mean": CLIP_MEAN, "std": CLIP_STD,
        "interpolation": "bicubic", "resize_mode": resize_mode, "fill_color": 0}), encoding="utf-8")
    tok = Tokenizer(models.WordLevel(VOCAB, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(folder / "textual" / "tokenizer.json"))
    (folder / "textual" / "tokenizer_config.json").write_text(
        json.dumps({"pad_token": "<pad>", "tokenizer_class": tokenizer_class}), encoding="utf-8")
    visual_onnx(folder / "visual" / "model.onnx", size, batch)
    textual_onnx(folder / "textual" / "model.onnx", words, extra_input)
    return folder
