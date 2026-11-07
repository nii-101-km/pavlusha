"""Convert test fixtures for independent assertions; not part of Office tools."""
from pathlib import Path
import subprocess
import tempfile


def convert(source, extension):
    source=Path(source)
    folder=Path(tempfile.mkdtemp(prefix='.verify-',dir=source.parent))
    profile=folder/'profile'
    result=subprocess.run(['/usr/bin/soffice','-env:UserInstallation='+profile.as_uri(),
        '--headless','--convert-to',extension,'--outdir',str(folder),str(source)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=60)
    output=folder/(source.stem+'.'+extension)
    if result.returncode or not output.is_file():
        raise RuntimeError('Test fixture conversion failed: '+result.stderr+' '+result.stdout)
    return output
