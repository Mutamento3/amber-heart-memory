"""The standard isolated preview, with an explicitly synthetic import model."""

from dataclasses import replace
import json
from threading import Lock
from time import sleep

from memory_web.app import create_app
from memory_web.config import WebSettings
from memory_web.models.extraction_model import ExtractionModel
from memory_web.testing import preview
from memory_web.testing.import_model import SyntheticImportFixed
from memory_web.testing.recent_notes import SyntheticSecretary


class BrowserImportFixed(SyntheticImportFixed):
    def __init__(self):
        super().__init__()
        self.lock=Lock()
        self.failures=0

    def extract(self,prompt):
        if '合成浏览器在途材料' in prompt:
            sleep(2)
        if '合成浏览器待重试材料' in prompt:
            with self.lock:
                self.failures+=1
                if self.failures<=2:
                    return '{}'
        return super().extract(prompt)


def import_preview_app():
    settings=WebSettings.from_env()
    path=settings.fixed_models_file
    fixtures=json.loads(path.read_text())
    fixtures.setdefault('embed',{})['合成记忆摘要。']=[1,0,0]
    path.write_text(json.dumps(fixtures,ensure_ascii=False))
    model=ExtractionModel(replace(settings,extraction_model='synthetic-import-model'),
        fixed_models=BrowserImportFixed())
    return create_app(settings,extraction_models=model,recent_note_models=SyntheticSecretary())


if __name__=='__main__':
    preview.preview_app=import_preview_app
    preview.main()
