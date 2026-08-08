"""Тесты чистой логики storyplan: правки редактора и манифест v2."""

from gpu_raid import storyplan


def test_merge_edit_basic():
    m = {"label": "x"}
    storyplan.merge_edit(m, {
        "order": [2, 0, 1],
        "excluded": [1, 1],
        "trims": {"0": {"in_s": 1.5}, "2": {"in_s": 0, "out_s": 0}},
        "crossfade_s": 0.5,
    })
    edit = m["edit"]
    assert edit["order"] == [2, 0, 1]
    assert edit["excluded"] == [1]
    # пустые тримы выбрасываются, ключи нормализуются в строки
    assert edit["trims"] == {"0": {"in_s": 1.5, "out_s": 0.0}}
    assert edit["crossfade_s"] == 0.5


def test_merge_edit_partial_keeps_rest():
    m = {"edit": {"order": [0, 1], "excluded": [1], "trims": {}, "crossfade_s": 1.0}}
    storyplan.merge_edit(m, {"crossfade_s": 0})
    assert m["edit"]["order"] == [0, 1]
    assert m["edit"]["excluded"] == [1]
    assert m["edit"]["crossfade_s"] == 0.0


def test_merge_edit_negative_crossfade_clamped():
    m = {}
    storyplan.merge_edit(m, {"crossfade_s": -3})
    assert m["edit"]["crossfade_s"] == 0.0


def test_segment_patch_prompt_dirty_on_done():
    seg = {"index": 0, "status": "done", "prompt": "старый"}
    storyplan.apply_segment_patch(seg, {"prompt": "новый"})
    assert seg["prompt"] == "новый"
    assert seg["dirty"] is True


def test_segment_patch_same_prompt_not_dirty():
    seg = {"index": 0, "status": "done", "prompt": "тот же"}
    storyplan.apply_segment_patch(seg, {"prompt": "тот же"})
    assert "dirty" not in seg


def test_segment_patch_pending_not_dirty():
    seg = {"index": 0, "status": "pending", "prompt": "a"}
    storyplan.apply_segment_patch(seg, {"prompt": "b"})
    assert "dirty" not in seg
    assert seg["prompt"] == "b"


def test_segment_patch_seed_and_duration():
    seg = {"index": 0, "status": "pending"}
    storyplan.apply_segment_patch(seg, {"seed": 2 ** 64 + 5, "duration_s": 0.01})
    assert seg["seed"] == 5              # приводится по модулю 2^64
    assert seg["duration_s"] == 0.1      # нижняя граница


def test_segment_patch_none_values_ignored():
    seg = {"index": 0, "status": "pending", "seed": 7}
    storyplan.apply_segment_patch(seg, {"seed": None, "duration_s": None})
    assert seg["seed"] == 7
    assert "duration_s" not in seg


def test_align_frames():
    assert storyplan.align_frames(5.0, 24, "minimax_h3") == 124   # шаблонная формула H3
    assert storyplan.align_frames(3.0, 24, "minimax_h3") == 73    # 72 -> 73 (73%17==5)
    assert storyplan.align_frames(0.1, 24, "minimax_h3") == 5     # минимум сетки
    assert storyplan.align_frames(5.0, 24, "none") == 120
    assert storyplan.align_frames(1.0, 30, "none") == 30


def test_canvas_h3():
    assert storyplan.canvas("16:9", 768, "minimax_h3") == (1344, 768)
    assert storyplan.canvas("9:16", 768, "minimax_h3") == (768, 1344)
    assert storyplan.canvas("1:1", 768, "minimax_h3") == (768, 768)
    w, h = storyplan.canvas("21:9", 768, "minimax_h3")
    assert w * h <= 768 * 1344 * 1.02   # кап площади (с допуском округления до 32)
    assert w % 32 == 0 and h % 32 == 0


def test_canvas_bad_aspect():
    try:
        storyplan.canvas("широкий", 768)
    except ValueError:
        pass
    else:
        raise AssertionError("ожидали ValueError")


def test_heuristic_split_deterministic():
    story = ("Рассвет над морем. Лодка отходит от берега.\n"
             "Шторм настигает героев. Волны бьют в борт. Молния.\n"
             "Утро. Тихая гавань.")
    a = storyplan.heuristic_split(story, target_segments=3)
    b = storyplan.heuristic_split(story, target_segments=3)
    assert a == b
    assert len(a["segments"]) == 3
    assert all(s["prompt"] for s in a["segments"])
    assert a["final_keyframe_prompt"]


def test_heuristic_split_auto_and_empty():
    auto = storyplan.heuristic_split("Одно предложение.")
    assert len(auto["segments"]) == 1
    empty = storyplan.heuristic_split("")
    assert empty["segments"] == []


def test_parse_llm_plan_dirty():
    raw = """Вот план:
```json
{"style": "cinematic, 35mm", "segments": [
  {"prompt": "boat departs", "keyframe_prompt": "boat at pier", "duration_s": "5"},
  {"prompt": "storm hits", "duration_s": null},
  {"no_prompt": true}
], "final_keyframe_prompt": "calm harbor"}
```"""
    plan = storyplan.parse_llm_plan(raw)
    assert plan["style"] == "cinematic, 35mm"
    assert len(plan["segments"]) == 2
    assert plan["segments"][0]["duration_s"] == 5.0
    assert plan["segments"][1]["duration_s"] is None
    assert plan["segments"][1]["keyframe_prompt"] == "storm hits"  # fallback на prompt
    assert plan["final_keyframe_prompt"] == "calm harbor"


def test_parse_llm_plan_garbage():
    for bad in ("", "просто текст", '{"segments": []}', '{"segments": [{"x": 1}]}'):
        try:
            storyplan.parse_llm_plan(bad)
        except ValueError:
            continue
        raise AssertionError(f"ожидали ValueError на {bad!r}")


def _vid():
    return {"fps": 24, "aspect": "16:9", "short_edge": 768, "snap": "minimax_h3",
            "segment_duration_s": 5.0, "width": 1344, "height": 768}


def test_new_story_manifest_structure():
    plan = {"style": "night city", "segments": [
        {"prompt": "a", "keyframe_prompt": "ka", "duration_s": None},
        {"prompt": "b", "keyframe_prompt": "kb", "duration_s": 3.0},
    ], "final_keyframe_prompt": "final"}
    m = storyplan.new_story_manifest("lbl", "story text", plan, _vid(), seed=100)
    assert m["schema"] == 2 and m["mode"] == "story" and m["state"] == "draft"
    assert len(m["segments"]) == 2
    assert len(m["keyframes"]) == 3          # N+1
    # стиль вшит в промпты
    assert m["segments"][0]["prompt"] == "night city. a"
    assert m["keyframes"][2]["prompt"] == "night city. final"
    # сегмент i соединяет кадры i и i+1
    assert m["segments"][1]["start_kf"] == 1 and m["segments"][1]["end_kf"] == 2
    # длительности: дефолт из spec и явная из плана
    assert m["segments"][0]["duration_s"] == 5.0
    assert m["segments"][0]["length_frames"] == 124
    assert m["segments"][1]["duration_s"] == 3.0
    assert m["segments"][1]["length_frames"] == 73
    # сиды детерминированы от базового
    assert m["segments"][0]["seed"] == 100
    assert m["segments"][1]["seed"] == 101
    assert m["keyframes"][0]["seed"] == 1100


def test_mark_stale():
    m = storyplan.new_story_manifest("lbl", "s", {
        "style": "", "segments": [{"prompt": "a", "keyframe_prompt": "a", "duration_s": None},
                                  {"prompt": "b", "keyframe_prompt": "b", "duration_s": None}],
        "final_keyframe_prompt": "f"}, _vid(), 0)
    m["segments"][0]["status"] = "done"
    m["segments"][1]["status"] = "done"
    touched = storyplan.mark_stale_for_keyframe(m, 1)   # общий кадр сегментов 0 и 1
    assert sorted(touched) == [0, 1]
    assert m["segments"][0]["stale"] and m["segments"][1]["stale"]
    m2 = storyplan.new_story_manifest("l", "s", {
        "style": "", "segments": [{"prompt": "a", "keyframe_prompt": "a", "duration_s": None}],
        "final_keyframe_prompt": "f"}, _vid(), 0)
    assert storyplan.mark_stale_for_keyframe(m2, 0) == []   # draft-сегменты не трогаем


def test_trim_manifest_view():
    m = {
        "label": "x",
        "segments": [{"index": 0}],
        "template_graph": {"1": {}},
        "spec_meta": {"start": "1"},
        "keyframe_template": {"2": {}},
        "keyframe_meta": {"prompt": "2"},
        "edit": {"order": []},
    }
    view = storyplan.trim_manifest_view(m)
    assert "template_graph" not in view
    assert "spec_meta" not in view
    assert "keyframe_template" not in view
    assert "keyframe_meta" not in view
    assert view["label"] == "x"
    assert view["segments"] == [{"index": 0}]
    assert view["edit"] == {"order": []}
    # исходный манифест не тронут
    assert "template_graph" in m
