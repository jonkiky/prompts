import json
import tempfile
from pathlib import Path
from skillhub.app import create_app

with tempfile.TemporaryDirectory() as tmp:
    spec=create_app(tmp,auth_mode='demo').openapi()
Path('docs/openapi.json').write_text(json.dumps(spec,indent=2)+'\n')
print('Wrote docs/openapi.json')
