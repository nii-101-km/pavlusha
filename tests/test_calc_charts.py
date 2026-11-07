from tests.office_test_conversion import convert
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.extensions import Event
from pavlusha_modules.office import create_extension
from pavlusha_modules.office.calc_adapter import LibreOfficeCalcAdapter, OfficeAdapterError, revision


class ChartToolTests(unittest.TestCase):
    def test_chart_tools_opt_in_and_reject_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            ext=create_extension(settings={'enable_calc':True}, context={'workdir':root,'output_limit':24000})
            tools={t.name:t for t in ext.on_event(Event('session_started','s',0,0)).tools}
            self.assertTrue({'ods_chart_create','ods_chart_list','ods_chart_export'} <= tools.keys())
            adapter=LibreOfficeCalcAdapter(root)
            with patch('subprocess.Popen') as worker:
                with self.assertRaises(OfficeAdapterError):
                    adapter.chart('source.ods','out.ods','bad','Data','A1:A5','D1','c','scatter')
                with self.assertRaises(OfficeAdapterError):
                    adapter.chart_export('source.ods','../chart.png','bad','Data')
                with self.assertRaises(OfficeAdapterError):
                    adapter.chart_export('source.ods','chart.png','bad','Data',0)
                worker.assert_not_called()
            ext.on_event(Event('session_stopped','s',0,0))


@unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_CALC')=='1','Set PAVLUSHA_TEST_CALC=1')
class ChartIntegrationTests(unittest.TestCase):
    def test_chart_roundtrip_and_png_export(self):
        from openpyxl import Workbook, load_workbook
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'plots').mkdir()
            book=Workbook(); sheet=book.active; sheet.title='Data'
            for row in [('Time','Distance'),(0,0),(1,2),(2,5),(3,9)]: sheet.append(row)
            book.save(root/'fixture.xlsx'); (root/'input.ods').write_bytes(convert(root/'fixture.xlsx','ods').read_bytes())
            adapter=LibreOfficeCalcAdapter(root)
            rev=revision(root/'input.ods')
            result=adapter.chart('input.ods','output.ods',rev,'Data','A1:B5','D2',
                'Experiment','scatter','Distance by time','Time','Distance')
            self.assertEqual(revision(root/'input.ods'),rev)
            loaded=load_workbook(convert(root/'output.ods','xlsx'))
            self.assertEqual(loaded['Data']['B5'].value,9)
            self.assertEqual(type(loaded['Data']._charts[0]).__name__,'ScatterChart')
            found=adapter.chart_list('output.ods',result['revision'],'Data')
            self.assertEqual(len(found['charts']),1)
            exported=adapter.chart_export('output.ods','plots/chart.png',result['revision'],'Data',1)
            self.assertEqual(exported['format'],'png')
            with Image.open(root/'plots/chart.png') as image:
                self.assertEqual(image.format,'PNG')
                self.assertGreater(image.width,300)
            with self.assertRaises(OfficeAdapterError):
                adapter.chart_export('output.ods','plots/chart.png',result['revision'],'Data',1)
            with self.assertRaises(OfficeAdapterError):
                adapter.chart_export('output.ods','plots/stale.png','old','Data',1)
