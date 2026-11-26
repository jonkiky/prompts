import json
import os
from pathlib import Path

import pytest
from pptx import Presentation

from skillhub.slide_builder import build
from skillhub.portable import convert


def test_portable_editable_deck(tmp_path):
    reference=Path(os.getenv('CCDI_SKILL_SOURCE','data/ccdi-source'))/'assets/reference.pptx'
    if not reference.exists():pytest.skip('Private CCDI reference is not distributed with code')
    spec={'slides':[{'kind':'cover','title':'Company Skill Hub','subtitle':'Editable workflow demonstration'}, {'kind':'flow','title':'From draft to trusted workflow','nodes':[{'label':'Draft','detail':'Author creates a workflow'},{'label':'Review','detail':'Independent review'},{'label':'Publish','detail':'Company discovery'}],'notes':'Demonstration input only; no business claims.'}]}
    path=build(reference,spec,tmp_path/'demo.pptx')
    deck=Presentation(path)
    assert len(deck.slides)==2
    assert any(s.has_text_frame and 'Draft' in s.text for s in deck.slides[1].shapes)
    assert any(s.has_text_frame and 'Company Skill Hub' in s.text for s in deck.slides[0].shapes)
    assert any(s.shape_type==13 for s in deck.slides[1].shapes), 'Original header logo is retained'
    assert 'no business claims' in deck.slides[1].notes_slide.notes_text_frame.text
