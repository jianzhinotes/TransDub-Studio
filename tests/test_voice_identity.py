from pathlib import Path

import numpy as np

from videotrans.dub.voice_identity import verify_voice_identity


def _queue(tmp_path):
    paths = {}
    for name in ("a-ref", "b-ref", "a-out", "b-out"):
        path = tmp_path / f"{name}.wav"
        path.write_bytes(b"audio")
        paths[name] = str(path)
    return paths, [
        {
            "filename": paths["a-out"], "speaker_id": "a",
            "cluster_ref_speaker_id": "a", "cluster_ref": paths["a-ref"],
            "speaker_identity_required": True,
        },
        {
            "filename": paths["b-out"], "speaker_id": "b",
            "cluster_ref_speaker_id": "b", "cluster_ref": paths["b-ref"],
            "speaker_identity_required": True,
        },
    ]


def test_voice_identity_passes_outputs_closest_to_own_anchor(tmp_path):
    paths, queue = _queue(tmp_path)
    vectors = {
        paths["a-ref"]: np.array([1.0, 0.0]),
        paths["b-ref"]: np.array([0.0, 1.0]),
        paths["a-out"]: np.array([0.95, 0.05]),
        paths["b-out"]: np.array([0.05, 0.95]),
    }

    report = verify_voice_identity(queue, embedding_fn=vectors.__getitem__)

    assert report["status"] == "passed"
    assert not report["failures"]


def test_voice_identity_rejects_output_closer_to_other_speaker(tmp_path):
    paths, queue = _queue(tmp_path)
    vectors = {
        paths["a-ref"]: np.array([1.0, 0.0]),
        paths["b-ref"]: np.array([0.0, 1.0]),
        paths["a-out"]: np.array([0.1, 0.9]),
        paths["b-out"]: np.array([0.0, 1.0]),
    }

    report = verify_voice_identity(queue, embedding_fn=vectors.__getitem__)

    failure = report["failures"][Path(paths["a-out"]).name]
    assert "cross_speaker_voice_match" in failure["hard_failures"]
