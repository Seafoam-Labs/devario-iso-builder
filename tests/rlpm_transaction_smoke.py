#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Run inside a private user/mount namespace; installs only a disposable probe.
from pathlib import Path
import tempfile,importlib.util,subprocess,tarfile,io,sys
spec=importlib.util.spec_from_file_location('builder',Path(__file__).resolve().parents[1] / 'builder.py');b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
host_config=Path('/etc/pacman.conf').read_bytes()
with tempfile.TemporaryDirectory(prefix='devario-rlpm-transaction-') as tmp:
 work=Path(tmp);root=work/'target';repo=work/'repo';repo.mkdir();root.mkdir()
 tarpath=repo/'devario-isolation-probe-1-1-any.pkg.tar'
 with tarfile.open(tarpath,'w') as tar:
  for name,data in {'.PKGINFO':b'pkgname = devario-isolation-probe\npkgver = 1-1\npkgdesc = Isolated transaction probe\nurl = https://example.invalid/\nbuilddate = 1\npackager = Devario test\nsize = 6\narch = any\nlicense = MIT\n','usr/share/devario-isolation-probe':b'probe\n'}.items():
   entry=tarfile.TarInfo(name);entry.mode=0o644;entry.size=len(data);tar.addfile(entry,io.BytesIO(data))
 subprocess.run(['zstd','-q',str(tarpath)],check=True)
 subprocess.run(['repo-add',str(repo/'probe.db.tar.gz'),str(tarpath)+'.zst'],check=True,stdout=subprocess.DEVNULL)
 source=work/'source.conf';source.write_text('[options]\nArchitecture = auto\nSigLevel = Never\n[probe]\nSigLevel = Never\nServer = file://'+str(repo)+'\n')
 config=work/'shelly.conf';b.isolated_config(source,root,work/'keys',config)
 for p in ['var/lib/pacman/local','var/cache/pacman/pkg','var/log','etc/pacman.d/hooks']:(root/p).mkdir(parents=True,exist_ok=True)
 (work/'keys').mkdir();cli=str(Path(sys.argv[1]).resolve())
 with b.shelly_configuration(config,work):
  b.command(cli,'sync','standard','--no-confirm')
  b.command(cli,'install','standard','--no-confirm','devario-isolation-probe')
  assert b.inventory(cli,config,['devario-isolation-probe'])=={'devario-isolation-probe':'1-1'}
 assert (root/'usr/share/devario-isolation-probe').read_text()=='probe\n'
 assert Path('/etc/pacman.conf').read_bytes()==host_config
 print('Published RLPM sync/install/inventory passed against a disposable target; host config unchanged.')
