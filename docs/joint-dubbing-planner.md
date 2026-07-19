# Joint dubbing planner

TransDub's joint planner treats translation, semantic grouping, target duration, speech synthesis,
and quality validation as one decision loop. The one-click production workflow now uses it before
TTS for the complete Chinese target queue and stores a resumable `.smart-plan` checkpoint. The
Dubbing Studio preview API remains non-destructive and does not replace production audio unless a
caller explicitly materializes the validated plan.

The Studio preview defaults to 20 units; the production workflow passes `limit=None` and plans the
complete queue:

1. Build source turns using speaker identity and pauses.
2. Compare original boundaries with conservative short-fragment merges.
3. Generate baseline, spoken-Chinese, and compact candidates without removing names or numbers.
   When DeepSeek is configured, send the complete source turn and all segmentation options in one
   structured request, then validate every returned ID, number, protected name, language, and
   compression ratio locally.
4. Predict duration using a model calibrated from existing speaker audio when available.
5. Synthesize the selected candidates in a batch through the existing TTS registry.
6. Validate language leakage and stretch ratio, then retry only the affected segment with a shorter
   semantically acceptable candidate.
7. Persist every decision and candidate in `dub_project.json`.

## Internal preview API

```python
from videotrans.task.joint_dub import run_joint_preview

project, plan = run_joint_preview(
    queue_tts=queue_tts,
    source_video=cfg.name,
    source_language=cfg.source_language_code,
    target_language=cfg.target_language_code,
    name=cfg.noextname,
    candidate_dir=f"{cfg.cache_folder}/joint_candidates",
    limit=20,
    tts_type=cfg.tts_type,
    uuid=cfg.uuid,
    is_cuda=cfg.is_cuda,
    synthesize=True,
    project_dir=f"{cfg.cache_folder}/joint-preview.tdproj",
    candidate_backend="auto",  # DeepSeek when configured; deterministic local rules otherwise
)
```

Use `synthesize=False` for a text/duration-only plan. The input `queue_tts` is copied and remains
unchanged. Candidate audio is kept separate from the production audio until a later render stage
explicitly accepts a validated plan.

`candidate_backend="deepseek"` requires the existing TransDub `deepseek_key` and
`deepseek_model` settings. It does not require an OpenAI key. Responses are cached under
`candidate_dir/llm_cache` using the model, prompt version, complete source turn, segmentation
options, time windows, and protected terms as the cache key. API errors, malformed JSON, mismatched
segment IDs, number loss, or excessive English fall back to the local rule generator without
interrupting the preview. Use `candidate_backend="rules"` for a fully offline run.

## Dubbing Studio preview

Dubbing Studio now starts smart planning automatically in the background shortly after the editor
opens. DeepSeek is used when its existing key/model are configured; any request or validation failure
falls back to deterministic local rules. If DeepSeek is not configured, the same default flow remains
fully offline. The editor stays usable while the first 20 lines are optimized.

When planning finishes, the single **View smart version** action opens the result. The window shows
the source, current wording, selected wording, target window,
predicted duration, stretch ratio, selected candidate kind, and all candidate scores in tooltips.
Rows above the preferred stretch threshold and hard duration threshold are highlighted separately.
DeepSeek partial/fallback counts are shown at the top. Double-clicking a row seeks the Studio video.

Planning is deliberately non-destructive: automatic startup planning does not replace
`StudioState`, subtitles, or existing audio. The plan history is persisted in
`dub_project.json` for the later A/B synthesis and explicit-acceptance stage.

For long videos (over five minutes), full-track waveform decoding and the expensive Python overlay
of hundreds of WAV clips are deferred at startup. Timeline blocks and per-segment listening remain
available immediately. This avoids the previous long macOS busy state on 20–30 minute interviews.

The result window also provides an explicit **Generate A/B audio** action. After confirmation it
synthesizes the already-selected plan through the current TTS backend; it does not call DeepSeek,
regenerate candidates, or change boundaries. Successful segments expose **Play current** and
**Play planned** controls. Candidate audio and actual-duration quality reports are persisted beside
the plan, while the production `queue_tts` audio selection remains unchanged.
