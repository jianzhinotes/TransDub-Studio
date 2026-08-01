from pathlib import Path

from videotrans.util.help_ffmpeg import format_video


def test_video_output_is_a_dedicated_child_of_output_root(tmp_path):
    source = tmp_path / 'interview.mp4'
    source.write_bytes(b'video')
    output_root = tmp_path / '_video_out'

    result = format_video(str(source), str(output_root))

    assert Path(result.target_dir) == output_root / 'interview-mp4'
    assert Path(result.target_dir).parent == output_root
