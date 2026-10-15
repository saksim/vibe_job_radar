"""Release gates reject mismatched or incomplete installed-package evidence."""
import copy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_python_release import validate_install


class PythonDistributionProofTests(unittest.TestCase):
    def test_report_reopen_and_source_identity_are_required_on_each_platform(self):
        manifest={'version':'0.7.0','source_revision':'a'*40,'source_sha256':'b'*64}
        wheel={'sha256':'c'*64}
        for platform in ('linux','win32','darwin'):
            proof={'success':True,'platform':platform,**manifest,'wheel_sha256':wheel['sha256'],
                'steps':['offline_install','five_installed_entrypoints'],
                'observations':[{'success':True,'stage':stage,'version':'0.7.0','platform':platform,
                    'installed_payload_verified':True,'external_requests':0}
                    for stage in ('import_and_original_report','independent_process_reopen')]}
            validate_install(proof,manifest,platform,wheel)
            variants=[]
            for field,value in [('success',False),('source_revision','d'*40),('wheel_sha256','e'*64),
                                ('platform','different'),('steps',['offline_install'])]:
                changed=copy.deepcopy(proof);changed[field]=value;variants.append(changed)
            changed=copy.deepcopy(proof);changed['observations'].pop();variants.append(changed)
            changed=copy.deepcopy(proof);changed['observations'][1]['success']=False;variants.append(changed)
            changed=copy.deepcopy(proof);changed['observations'][0]['external_requests']=1;variants.append(changed)
            for changed in variants:
                with self.subTest(platform=platform,changed=changed),self.assertRaises(ValueError):
                    validate_install(changed,manifest,platform,wheel)


if __name__=='__main__':unittest.main()
