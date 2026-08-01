import json
from pathlib import Path

from videotrans.util import help_srt


def test_bilingual_bottom_line_uses_its_own_font(monkeypatch, tmp_path):
    """双语字幕的中文行必须采用 Bottom_Fontname，而非复用英文行字体。"""
    styles_dir = tmp_path / 'videotrans'
    styles_dir.mkdir()
    (styles_dir / 'ass.json').write_text(json.dumps({
        'Fontname': 'English Font',
        'Bottom_Fontname': 'Chinese Font',
        'Fontsize': 15,
        'Bottom_Fontsize': 18,
    }), encoding='utf-8')
    source = tmp_path / 'subtitles.srt'
    source.write_text('1\n00:00:00,000 --> 00:00:01,000\nEnglish line###中文行\n', encoding='utf-8')

    def fake_ffmpeg(args):
        Path(args[-1]).write_text(
            '[V4+ Styles]\n'
            'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n'
            'Style: Default,Arial,16,&H00FFFFFF&,&H00FFFFFF&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n'
            '[Events]\n'
            'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
            'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,English line###中文行\n',
            encoding='utf-8')

    monkeypatch.setattr(help_srt, 'ROOT_DIR', str(tmp_path))
    monkeypatch.setattr('videotrans.util.help_ffmpeg.runffmpeg', fake_ffmpeg)
    output = Path(help_srt.set_ass_font(str(source)))
    rendered = output.read_text(encoding='utf-8')

    assert 'Style: Default,English Font,15,' in rendered
    assert 'Style: Bottom,Chinese Font,18,' in rendered
    assert '{\\rBottom}中文行{\\r}' in rendered
