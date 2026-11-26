"""Run with Python 3.11 -I -S before pip install; fake SSH never contacts a server."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from infra.audit import sample_diagnostics as d

SHA="a"*40
FP="SHA256:"+"A"*43


class CleanRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch=tempfile.TemporaryDirectory()
        cls.base=Path(cls.scratch.name)
        cls.envdir=cls.base/"python"
        venv.EnvBuilder(with_pip=False).create(cls.envdir)
        cls.python=cls.envdir/"bin/python"

    @classmethod
    def tearDownClass(cls): cls.scratch.cleanup()

    def environment(self,root):
        runner=root/"runner"; runner.mkdir()
        project=root/"project"; project.mkdir()
        return dict(os.environ,PATH=str(self.envdir/"bin")+os.pathsep+os.environ["PATH"],
            OPERATION="stage",REVIEWED_SHA=SHA,SELECTED_SHA=SHA,STAGE_TICKET="gh-1-1",
            PROJECT_PATH=str(project),ALLOW_EXISTING_ROOT="true",BATCH_OFFSET="0",BATCH_SIZE="2",
            PLAN_PATH="",MANIFEST_PATH="",AFTER_UUID="",RUNNER_TEMP=str(runner),
            GITHUB_STEP_SUMMARY=str(root/"summary"),SSH_HOST="fixture.invalid",SSH_USER="fixture",
            SSH_KEY="PRIVATE_FIXTURE_KEY_NEVER_LOG",SSH_FINGERPRINT=FP,
            GH_TOKEN="FIXTURE_TOKEN_NEVER_FORWARD",GITHUB_TOKEN="FIXTURE_TOKEN_NEVER_FORWARD")

    def test_clean_request_current_and_historical_without_project_dependencies(self):
        baseline=os.environ.get("BASELINE_DIR")
        repositories=[ROOT]+([Path(baseline).resolve()] if baseline else [])
        for repository in repositories:
            with self.subTest(repository="historical" if repository!=ROOT else "current"),tempfile.TemporaryDirectory(dir=self.base) as tmp:
                env=self.environment(Path(tmp))
                probe=subprocess.run([str(self.python),"-I","-S","-c",
                    "import importlib.util; assert all(importlib.util.find_spec(x) is None for x in ('boto3','psycopg2','pyarrow','tennis_predictor'))"],
                    capture_output=True,text=True,env=env)
                self.assertEqual(probe.returncode,0,probe.stderr)
                result=subprocess.run([str(self.python),"-I","-S",str(repository/"scripts/sample_workflow_request.py")],
                    cwd=repository,env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn("raise SystemExit(main(",result.stdout)
                compile(result.stdout,"reviewed-payload","exec")
                self.assertNotIn("Traceback",result.stderr)
                self.assertNotIn(env["SSH_KEY"],result.stdout+result.stderr)

    def fake_commands(self,root,env):
        directory=root/"commands"; directory.mkdir()
        def command(name,body):
            path=directory/name
            path.write_text("#!"+str(self.python)+"\n"+body)
            path.chmod(0o700)
        command("ssh-keyscan", "print('fixture.invalid ssh-ed25519 fixture')\n")
        command("ssh-keygen", "import os\nprint('256 '+os.environ.get('FAKE_FP',"+repr(FP)+")+' fixture (ED25519)')\n")
        command("ssh", r"""import os,sys,json,subprocess
assert "GH_TOKEN" not in os.environ and "GITHUB_TOKEN" not in os.environ
payload=sys.stdin.buffer.read()
mode=os.environ.get("FAKE_MODE","ok")
if mode=="auth":
    print("fixture@PRIVATE_HOST: Permission denied (publickey).",file=sys.stderr)
    sys.exit(255)
if mode=="error":
    print("sample_diag:remote_entry:ok:none",file=sys.stderr)
    print("sample_diag:postgres_selection:error:selection_plan",file=sys.stderr)
    print("PRIVATE_ROW postgres://PRIVATE_PASSWORD@HOST/path",file=sys.stderr)
    sys.exit(1)
if mode=="runtime":
    result=subprocess.run(["bash","-c",sys.argv[-1]],input=payload,capture_output=True)
    sys.stdout.buffer.write(result.stdout); sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
print("sample_diag:remote_entry:ok:none",file=sys.stderr)
if mode=="capture":
    print("PRIVATE_ROW_NOT_JSON")
else:
    print(json.dumps({"ticket":os.environ["STAGE_TICKET"],"archive_sha256":"b"*64,
        "source_sha":os.environ["REVIEWED_SHA"],"created_at":1,"exported_count":2}))
""")
        env["PATH"]=str(directory)+os.pathsep+env["PATH"]

    def run_transport(self,mode="ok",changes=None):
        with tempfile.TemporaryDirectory(dir=self.base) as tmp:
            root=Path(tmp); env=self.environment(root); self.fake_commands(root,env)
            env["FAKE_MODE"]=mode
            if changes: env.update(changes)
            if mode=="runtime":
                venvbin=Path(env["PROJECT_PATH"])/".venv/bin"; venvbin.mkdir(parents=True)
                (venvbin/"python").symlink_to(self.python)
            result=subprocess.run(["bash",str(ROOT/"infra/audit/sample_ssh.sh")],
                cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
            summary=(root/"summary").read_text() if (root/"summary").exists() else ""
            self.assertFalse(list(Path(env["RUNNER_TEMP"]).glob("sample-ssh.*")))
            combined=result.stdout+result.stderr+summary
            for forbidden in ("PRIVATE_ROW","PRIVATE_PASSWORD","PRIVATE_HOST","PRIVATE_FIXTURE_KEY",
                              "FIXTURE_TOKEN","Traceback",env["PROJECT_PATH"]):
                self.assertNotIn(forbidden,combined)
            return result,summary

    def test_transport_success(self):
        result,summary=self.run_transport()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("sample_diag:runner_capture:ok:none",result.stderr)
        self.assertIn("b"*64,summary)

    def test_remote_reason_survives_nonzero_pipeline_and_no_success_summary(self):
        result,summary=self.run_transport("error")
        self.assertNotEqual(result.returncode,0)
        self.assertIn("sample_diag:postgres_selection:error:selection_plan",result.stderr)
        self.assertIn("sample_diag:runner_capture:skipped:upstream_failed",result.stderr)
        self.assertEqual(summary,"")

    def test_fingerprint_auth_capture_and_input_failures(self):
        for mode,changes,expected in [
            ("ok",{"FAKE_FP":"SHA256:"+"B"*43},"host_verify:error:fingerprint_mismatch"),
            ("auth",{},"ssh_transport:error:auth_denied"),
            ("capture",{},"runner_capture:error:malformed_output"),
            ("ok",{"BATCH_SIZE":"6"},"runner_request:error:invalid_input"),
            ("ok",{"SSH_KEY":""},"runner_preflight:error:ssh_settings_missing"),
        ]:
            with self.subTest(expected=expected):
                result,summary=self.run_transport(mode,changes)
                self.assertNotEqual(result.returncode,0)
                self.assertIn("sample_diag:"+expected,result.stderr)
                self.assertEqual(summary,"")

    def test_real_streamed_bootstrap_on_dependency_free_fake_server(self):
        result,summary=self.run_transport("runtime")
        self.assertNotEqual(result.returncode,0)
        self.assertIn("sample_diag:remote_bootstrap:ok:none",result.stderr)
        self.assertIn("sample_diag:dependency_psycopg2:error:dependency_missing",result.stderr)
        self.assertEqual(summary,"")

    def test_relay_exact_allowlist_and_limits(self):
        output=io.StringIO()
        d.relay(io.BytesIO(b"sample_diag:remote_config:error:config_missing\n"
            b"sample_diag:remote_config:error:PRIVATE_SECRET\n"
            b"sample_diag:remote_config:error:config_missing PRIVATE_SECRET\n"
            b"PRIVATE_ROW\n"+b"x"*600+b"\n"),output)
        self.assertEqual(output.getvalue(),"sample_diag:remote_config:error:config_missing\n")
        output=io.StringIO()
        d.relay(io.BytesIO(b"x"*1000),output,limit=32)
        self.assertIn("output_limit",output.getvalue())

    def test_exception_reason_does_not_reflect_exception_text(self):
        self.assertEqual(d.reason("remote_config",ValueError("PRIVATE_SECRET")),"config_invalid")
        class SQLFailure(Exception): pgcode="57014"
        self.assertEqual(d.reason("postgres_selection",SQLFailure("PRIVATE_SQL")),"query_timeout")
        self.assertEqual(d.reason("remote_dependencies",ImportError("PRIVATE_PATH")),"imports_failed")


    def test_oversized_single_line_at_eof_reports_bound(self):
        output=io.StringIO()
        d.relay(io.BytesIO(b"x"*1000),output,limit=700)
        self.assertEqual(output.getvalue(),"sample_diag:diagnostic_channel:error:output_limit\n")

    def test_bootstrap_validation_never_reflects_payload(self):
        with tempfile.TemporaryDirectory(dir=self.base) as tmp:
            env=self.environment(Path(tmp))
            payload=base64.b64encode(json.dumps({"request":{"allow_existing_root":True},
                "files":{"PRIVATE_PATH":"PRIVATE_ROW"},"sha":SHA,"project":env["PROJECT_PATH"]}).encode()).decode()
            code="import sys;sys.path.insert(0,"+repr(str(ROOT))+");from infra.audit.sample_acquisition_bootstrap import main;sys.exit(main("+repr(payload)+"))"
            result=subprocess.run([str(self.python),"-I","-S","-c",code],
                capture_output=True,text=True,env=env)
            self.assertEqual(result.returncode,1)
            self.assertIn("sample_diag:remote_bootstrap:error:payload_invalid",result.stderr)
            self.assertNotIn("PRIVATE",result.stderr)
            self.assertNotIn("Traceback",result.stderr)


if __name__=="__main__": unittest.main()
