"""Portable editable CCDI slide authoring. Bundled with the imported skill."""
import argparse
import io
import json
from copy import deepcopy
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

BLUE, MEDIUM, PALE, TEXT = '2D6483', '508BAC', 'E7F1F6', '343F47'


def clone_shape(source, slide):
    node = deepcopy(source.element)
    for blip in node.iter(qn('a:blip')):
        old = blip.get(qn('r:embed'))
        if old:
            part = source.part.related_part(old)
            _, new_id = slide.part.get_or_add_image_part(io.BytesIO(part.blob))
            blip.set(qn('r:embed'), new_id)
    # Template header shapes contain no external hyperlinks or linked media.
    for hlink in list(node.iter(qn('a:hlinkClick'))):
        hlink.getparent().remove(hlink)
    slide.shapes._spTree.insert_element_before(node, 'p:extLst')
    return slide.shapes[-1]


def text(slide, value, x, y, w, h, size=18, color=TEXT, bold=False, align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    for i, line in enumerate(str(value).split('\n')):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.alignment = align
        p.font.name = 'Montserrat'
        p.font.size = Pt(size)
        p.font.bold = bold
        p.font.color.rgb = RGBColor.from_string(color)
    return box


def rect(slide, x, y, w, h, fill=PALE, border='C9DDE8', kind=MSO_SHAPE.ROUNDED_RECTANGLE):
    s = slide.shapes.add_shape(kind, Inches(x), Inches(y), Inches(w), Inches(h))
    s.fill.solid(); s.fill.fore_color.rgb = RGBColor.from_string(fill)
    s.line.color.rgb = RGBColor.from_string(border)
    return s


def build(reference, spec, output):
    source = Presentation(reference)
    out = Presentation(reference)
    for sid in list(out.slides._sldIdLst):
        out.part.drop_rel(sid.rId)
        out.slides._sldIdLst.remove(sid)
    slides = spec.get('slides', [])
    if not 1 <= len(slides) <= 40:
        raise ValueError('Provide 1 to 40 slides')
    for idx, item in enumerate(slides):
        title = item.get('title', '')
        if not title or len(title) > 120:
            raise ValueError('Every slide needs a concise title under 120 characters')
        layout = min(out.slide_layouts, key=lambda candidate: len(candidate.placeholders))
        slide = out.slides.add_slide(layout)
        for placeholder in list(slide.shapes):
            placeholder.element.getparent().remove(placeholder.element)
        cover = item.get('kind') == 'cover'
        if cover:
            for shape in source.slides[0].shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or (shape.top < Inches(1.1) and shape.name != 'cover-title'):
                    clone_shape(shape, slide)
                elif shape.width > Inches(13) and shape.height > Inches(7):
                    clone_shape(shape, slide)
            text(slide, title, 4.17, 3.70, 8.2, 1.5, 29, 'FFFFFF', True, PP_ALIGN.RIGHT)
            text(slide, item.get('subtitle', ''), 5.3, 5.3, 7.0, .9, 16, 'FFFFFF', align=PP_ALIGN.RIGHT)
            text(slide, item.get('date', ''), 9.1, 6.6, 3.2, .35, 12, 'FFFFFF', align=PP_ALIGN.RIGHT)
        else:
            for shape in list(source.slides[1].shapes)[:5]:
                cloned = clone_shape(shape, slide)
                if shape.name in ('slide-title', 'slide-number'):
                    cloned.text = title if shape.name == 'slide-title' else str(idx+1)
                    for p in cloned.text_frame.paragraphs:
                        p.alignment = PP_ALIGN.CENTER
                        p.font.name = 'Montserrat SemiBold'
                        p.font.size = Pt(18.75 if shape.name == 'slide-title' else 14)
                        p.font.color.rgb = RGBColor.from_string(MEDIUM if shape.name == 'slide-title' else 'FFFFFF')
            nodes = item.get('nodes', [])
            if not 1 <= len(nodes) <= 5:
                raise ValueError('Content slides need 1 to 5 diagram nodes')
            if any(len(n.get('label', '')) > 60 or len(n.get('detail', '')) > 160 for n in nodes):
                raise ValueError('Shorten node labels and detail or split the slide')
            width = (11.7 - .32*(len(nodes)-1))/len(nodes)
            y, height = 2.25, 2.9
            for ni, node in enumerate(nodes):
                x = .8 + ni*(width+.32)
                rect(slide, x, y, width, height)
                text(slide, node.get('label', ''), x+.14, y+.28, width-.28, .8, 17, BLUE, True, PP_ALIGN.CENTER)
                text(slide, node.get('detail', ''), x+.16, y+1.16, width-.32, 1.4, 12.5, TEXT, align=PP_ALIGN.CENTER)
                if ni < len(nodes)-1 and item.get('kind', 'flow') == 'flow':
                    rect(slide, x+width+.035, y+1.27, .25, .23, MEDIUM, MEDIUM, MSO_SHAPE.RIGHT_ARROW)
            if item.get('context'):
                text(slide, item['context'], .8, 1.35, 11.7, .6, 15, TEXT)
            if item.get('takeaway'):
                rect(slide, .8, 6.42, 11.7, .65, BLUE, BLUE, MSO_SHAPE.RECTANGLE)
                text(slide, item['takeaway'], 1.0, 6.53, 11.3, .44, 14, 'FFFFFF', align=PP_ALIGN.CENTER)
        slide.notes_slide.notes_text_frame.text = item.get('notes', '')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    out.save(output)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference', type=Path, default=Path(__file__).resolve().parents[1]/'assets/reference.pptx')
    args = parser.parse_args()
    print(build(args.reference, json.loads(args.input.read_text()), args.output))
