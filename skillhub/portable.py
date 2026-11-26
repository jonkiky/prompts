from pathlib import Path


def convert(original):
    files = dict(original)
    files['SKILL.md'] = b'''---
name: artifact-template-ccdi-slide-generation
description: Create or restyle CCDI presentations using the retained Frederick CCDI visual style, with editable diagrams and minimal slide text. Use for CCDI-style decks in Codex, Cursor or Copilot when the required Python presentation runtime is available.
---

# CCDI Slide Generation

Read references/style-guide.md and inspect assets/reference.pptx and the preview images. Preserve the original logos, photography, palette, typography, and header treatment. The reference provides styling, not subject matter. Follow the current user's audience, content, slide count, and newer template choices. Do not reuse old claims or dates as new evidence.

This portable edition explicitly offers an editable PowerPoint workflow using scripts/build_deck.py. It does not require the OpenAI Presentations plugin. If a client has a better native editable authoring workflow, it may use that while following the same style guide and verification requirements.

Plan each slide around one visual relationship. Write a JSON input using references/deck-schema.md, then run the bundled script with the Python executable returned by the company connector:

    python scripts/build_deck.py --input /absolute/path/deck.json --output /absolute/path/ccdi.pptx

Resolve the script and reference paths from this skill's local directory. The script preserves template header assets and makes diagrams and labels editable. It does not invent facts, numerical data, or relationships. Put detail and sources in speaker notes. Do not shrink a crowded slide; split or simplify it.

Render every finished slide with an available PowerPoint renderer such as LibreOffice and inspect the images. Check branding, readable labels, overlap, clipping, and editable slide objects. If the renderer or required font is unavailable, label the deliverable a draft and state the verification gap. Do not claim that visual QA passed without rendering. Respect client file-delivery conventions and the user's permissions for external actions.
'''
    files['scripts/build_deck.py'] = Path(__file__).with_name('slide_builder.py').read_bytes()
    files['references/deck-schema.md'] = b'''# Portable deck input

Provide a JSON object with a slides array. Each slide has title and optional notes. A cover uses kind=cover, subtitle and date. Other slides use kind=flow or kind=comparison and one to five nodes, each with label and optional detail. Optional context appears above the diagram; optional takeaway appears in the footer. Keep titles under 120 characters, labels under 60 and detail under 160. Use only facts supplied by the user or their sources. The renderer uses native editable PowerPoint shapes and the retained branding assets.

For example, a flow slide can have a title of Review process and three nodes labeled Draft, Review and Publish. Those stages are an example of structure, not mandatory content for a new deck.
'''
    return files, {'display_name':'CCDI Slide Generation', 'dependencies':['python:pptx'], 'compatibility': {c:'unverified' for c in ('codex','cursor','copilot')}, 'workflow':'portable-editable-pptx', 'validation':'Portable builder is testable independently; native agent routing still requires the client trial.'}
