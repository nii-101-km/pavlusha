"""Short-lived UNO worker, run by the Python installation providing pyuno."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.connection import NoConnectException


def prop(name, value):
    p = PropertyValue()
    p.Name, p.Value = name, value
    return p


def main(request):
    pipe = 'pavlusha_' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='pavlusha-lo-profile-') as profile:
        proc = subprocess.Popen([request['soffice'], '-env:UserInstallation=' + Path(profile).as_uri(),
            '--headless', '--norestore', '--nodefault', '--nofirststartwizard',
            '--accept=pipe,name=' + pipe + ';urp;StarOffice.ComponentContext'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        doc = None
        try:
            local = uno.getComponentContext()
            resolver = local.ServiceManager.createInstanceWithContext('com.sun.star.bridge.UnoUrlResolver', local)
            for _ in range(200):
                try:
                    ctx = resolver.resolve('uno:pipe,name=' + pipe + ';urp;StarOffice.ComponentContext')
                    break
                except NoConnectException:
                    if proc.poll() is not None:
                        raise RuntimeError('LibreOffice stopped during startup')
                    time.sleep(.1)
            else:
                raise RuntimeError('LibreOffice startup timed out')
            desktop = ctx.ServiceManager.createInstanceWithContext('com.sun.star.frame.Desktop', ctx)
            source = 'private:factory/scalc' if request['action']=='create' else Path(request['source']).as_uri()
            doc = desktop.loadComponentFromURL(source, '_blank', 0,
                (prop('Hidden', True), prop('ReadOnly', request['action'] in ('inspect','read','chart_export','chart_list')),
                 prop('MacroExecutionMode', 0), prop('UpdateDocMode', 0)))
            if doc is None:
                raise RuntimeError('Could not open workbook')
            if request['action'] == 'create':
                doc.Sheets.getByIndex(0).Name=request['sheet']
                doc.storeAsURL(Path(request['output']).as_uri(),
                    (prop('FilterName','calc8'),prop('Overwrite',False)))
                return {'sheet':request['sheet']}
            if request['action'] == 'inspect':
                sheets=[]
                for name in doc.Sheets.ElementNames:
                    sheet=doc.Sheets.getByName(name)
                    cursor=sheet.createCursor();cursor.gotoEndOfUsedArea(True)
                    area=cursor.getRangeAddress()
                    sheets.append({'name':name,'rows':area.EndRow+1,'columns':area.EndColumn+1})
                return {'sheets':sheets}
            if request['action'] == 'read':
                sheet = doc.Sheets.getByName(request['sheet'])
                area = sheet.getCellRangeByName(request['range'])
                addr = area.getRangeAddress()
                cells = []
                for row in range(addr.StartRow, addr.EndRow + 1):
                    for col in range(addr.StartColumn, addr.EndColumn + 1):
                        cell = sheet.getCellByPosition(col, row)
                        kind = cell.Type.value
                        cells.append({'row': row + 1, 'column': col + 1, 'type': kind,
                            'formula': cell.Formula if kind == 'FORMULA' else None,
                            'value': cell.Value if kind in ('VALUE', 'FORMULA') else (cell.String if kind == 'TEXT' else None),
                            'display': cell.String, 'error': cell.Error})
                return {'cells': cells}
            if request['action'] == 'chart':
                sheet = doc.Sheets.getByName(request['sheet'])
                charts = sheet.getCharts()
                name = request['name']
                if charts.hasByName(name):
                    raise RuntimeError('Chart name already exists')
                anchor = sheet.getCellRangeByName(request['anchor']).getCellAddress()
                if anchor.Column > 255 or anchor.Row > 999:
                    raise RuntimeError('Chart anchor must be within A1:IV1000')
                rect = uno.createUnoStruct('com.sun.star.awt.Rectangle')
                rect.X = sum(sheet.Columns.getByIndex(i).Width for i in range(anchor.Column))
                rect.Y = sum(sheet.Rows.getByIndex(i).Height for i in range(anchor.Row))
                rect.Width, rect.Height = 16000, 9000
                source = sheet.getCellRangeByName(request['cell_range']).getRangeAddress()
                charts.addNewByName(name, rect, (source,), request['headers'], request['type'] != 'scatter')
                chart = charts.getByName(name).getEmbeddedObject()
                diagram_type = {'scatter':'XYDiagram', 'line':'LineDiagram', 'column':'BarDiagram', 'pie':'PieDiagram'}[request['type']]
                chart.setDiagram(chart.createInstance('com.sun.star.chart.' + diagram_type))
                chart.HasMainTitle = bool(request['title'])
                if request['title']:
                    chart.Title.String = request['title']
                chart.HasLegend = request['legend']
                diagram = chart.getDiagram()
                if request['type'] == 'scatter':
                    diagram.Lines = False
                if request['type'] != 'pie':
                    if request['x_title']:
                        diagram.HasXAxisTitle = True
                        diagram.XAxisTitle.String = request['x_title']
                    if request['y_title']:
                        diagram.HasYAxisTitle = True
                        diagram.YAxisTitle.String = request['y_title']
                doc.storeAsURL(Path(request['output']).as_uri(),
                    (prop('FilterName', 'calc8'), prop('Overwrite', False)))
                return {'chart': name, 'type': request['type']}
            if request['action'] == 'chart_list':
                sheet = doc.Sheets.getByName(request['sheet'])
                draw = sheet.getDrawPage()
                shapes = []
                for i in range(draw.Count):
                    shape = draw.getByIndex(i)
                    if shape.supportsService('com.sun.star.drawing.OLE2Shape') and sheet.getCharts().hasByName(shape.PersistName):
                        shapes.append({'index':len(shapes)+1,'name':shape.PersistName})
                return {'charts':shapes}
            if request['action'] == 'chart_export':
                sheet = doc.Sheets.getByName(request['sheet'])
                draw = sheet.getDrawPage()
                shapes = []
                for i in range(draw.Count):
                    candidate = draw.getByIndex(i)
                    if candidate.supportsService('com.sun.star.drawing.OLE2Shape') and sheet.getCharts().hasByName(candidate.PersistName):
                        shapes.append(candidate)
                index = request['chart_index']
                if index > len(shapes):
                    raise RuntimeError('Chart index not found on sheet')
                shape = shapes[index - 1]
                exporter = ctx.ServiceManager.createInstanceWithContext('com.sun.star.drawing.GraphicExportFilter', ctx)
                exporter.setSourceDocument(shape)
                if not exporter.filter((prop('MediaType', 'image/png'), prop('URL', Path(request['output']).as_uri()))):
                    raise RuntimeError('Chart export failed')
                return {'chart_index': index, 'format': 'png'}
            for edit in request['edits']:
                cell = doc.Sheets.getByName(edit['sheet']).getCellRangeByName(edit['cell'])
                if 'formula' in edit:
                    cell.Formula = edit['formula']
                elif 'date' in edit:
                    from datetime import date
                    value=date.fromisoformat(edit['date'])
                    origin=doc.NullDate
                    cell.Value=float((value-date(origin.Year,origin.Month,origin.Day)).days)
                    formats=doc.NumberFormats
                    locale=uno.createUnoStruct('com.sun.star.lang.Locale')
                    pattern='YYYY-MM-DD'
                    key=formats.queryKey(pattern,locale,True)
                    if key<0: key=formats.addNew(pattern,locale)
                    cell.NumberFormat=key
                elif edit['value'] is None:
                    cell.clearContents(23)
                elif isinstance(edit['value'], str):
                    cell.String = edit['value']
                else:
                    cell.Value = float(edit['value'])
            doc.calculateAll()
            doc.storeAsURL(Path(request['output']).as_uri(),
                (prop('FilterName', 'calc8'), prop('Overwrite', False)))
            return {'edited': len(request['edits']), 'recalculated': True}
        finally:
            if doc is not None:
                try:
                    doc.close(True)
                except Exception:
                    pass
            # The process has its own profile and session, never an existing desktop.
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    try:
        print(json.dumps(main(json.load(sys.stdin)), ensure_ascii=False, allow_nan=False))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
