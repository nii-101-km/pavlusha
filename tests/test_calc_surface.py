import os
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from pavlusha_agent.extensions import Event
from pavlusha_modules.office import Office, create_extension
from pavlusha_modules.office.surface_adapter import create_surface


class SurfaceTests(unittest.TestCase):
    def test_opt_in_and_invalid_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            office=create_extension(settings={'enable_calc':True},context={'workdir':tmp,'output_limit':24000})
            tools={tool.name for tool in office.on_event(Event('session_started','s',0,0)).tools}
            self.assertIn('calc_surface_create',tools)
            self.assertIn('calc_ods_inspect',tools)
            office.on_event(Event('session_stopped','s',0,0))
            result=create_surface(tmp,path='../input.ods',output_path='out.ods',image_path='out.png',
                revision='bad',sheet='Sheet1',cell_range='A1:C3',anchor='E1')
            self.assertFalse(result['ok'])


@unittest.skipUnless(os.environ.get('PAVLUSHA_TEST_CALC')=='1','Set PAVLUSHA_TEST_CALC=1')
class SurfaceIntegrationTests(unittest.TestCase):
    def test_surface_is_embedded_and_source_unchanged(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); office=Office(root,24000)
            code='''Sub Main
Dim s As Object, i As Integer, j As Integer
s=ThisComponent.Sheets(0)
For i=0 To 2
s.getCellByPosition(i+1,0).Value=i
s.getCellByPosition(0,i+1).Value=i
Next i
For i=0 To 2
For j=0 To 2
s.getCellByPosition(i+1,j+1).Value=i*i+j*j
Next j
Next i
End Sub'''
            self.assertTrue(office.basic_macro(code,'grid.ods')['ok'])
            before=(root/'grid.ods').read_bytes()
            preview=office.inspect_ods(path='grid.ods',sheet='Sheet1',cell_range='A1:C3')
            self.assertTrue(preview['ok'],preview)
            self.assertEqual(preview['cells'][1]['value'],0)
            args=dict(path='grid.ods',output_path='output.ods',image_path='surface.png',revision=preview['revision'],
                sheet='Sheet1',cell_range='A1:D4',anchor='F2',title='z=x²+y²')
            result=office.create_surface(**args)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['z_max'],8)
            self.assertEqual((root/'grid.ods').read_bytes(),before)
            with ZipFile(root/'output.ods') as archive:
                self.assertTrue(any(n.startswith('Pictures/') and n.endswith('.png') for n in archive.namelist()))
                self.assertIn(b'draw:image',archive.read('content.xml'))
            with Image.open(root/'surface.png') as image:
                self.assertEqual(image.size,(1200,900))
            self.assertFalse(office.create_surface(**args)['ok'])
            args.update(output_path='stale.ods',image_path='stale.png',revision='old')
            self.assertFalse(office.create_surface(**args)['ok'])
            self.assertFalse((root/'stale.ods').exists())
