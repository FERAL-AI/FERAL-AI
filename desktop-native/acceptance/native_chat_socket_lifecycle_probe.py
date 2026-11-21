"""Compile/run native receipt tests; temporary same-file private access only."""
from pathlib import Path
import argparse
import re
import subprocess
import sys
import tempfile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
parser.add_argument("--output", type=Path)
args = parser.parse_args()
src = args.source_root.resolve() / "desktop-native"
tmp = args.output or Path(tempfile.mkdtemp(prefix="feral-native-socket-fixture-"))
tmp.mkdir(parents=True, exist_ok=True)
label = "current"
api = src / "APIModel.swift"
compiled=tmp/('APIModel-'+label+'.swift')
compiled.write_text(api.read_text()+'''
#if FERAL_NATIVE_SOCKET_FIXTURE
extension NativeModel {
    func fixtureAttachChatSocket(_ observed: URLSessionWebSocketTask) { socket = observed }
    func fixtureRestoreUnknownReference(_ reference: NativeTurnReference) {
        var marker = reference.record; marker["state"] = "outcome_unknown"
        messages.append(NativeMessage(id: UUID().uuidString, role: "user", text: "Old synthetic request", metadata: ["chat_turn": marker]))
        recoverSavedChatRequest()
    }
    func fixturePrepareChatSocket(_ observed: URLSessionWebSocketTask) {
        socket = observed
        chatTurns.configure(sessionID: conversationID, connectionID: socketGeneration)
        configureContext(sessionID: conversationID, connectionID: socketGeneration, required: false, requestID: chatTurns.capabilityID)
    }
    func fixtureSocketClosed(_ observed: URLSessionWebSocketTask) {
        transportDelegate.urlSession(session, webSocketTask: observed, didCloseWith: .goingAway, reason: nil)
    }
    func fixtureSocketCompleted(_ observed: URLSessionWebSocketTask) {
        (transportDelegate as URLSessionTaskDelegate).urlSession?(session, task: observed, didCompleteWithError: nil)
    }
    func fixtureHTTPCompleted(_ observed: URLSessionDataTask) {
        (transportDelegate as URLSessionTaskDelegate).urlSession?(session, task: observed, didCompleteWithError: nil)
    }
}
#endif
''')
script=(src/'test_features.sh').read_text()
section=script.split('model_binary=/private/tmp/theora-native-model-wire-tests',1)[1].split('"$model_binary"',1)[0]
names=re.findall(r'(?<![\w/])\w+\.swift',section)
assert len(names)>50
files=[]
for name in names:
    if name=='APIModel.swift': files.append(str(compiled))
    else: files.append(str(src/name))
binary=tmp/('native-model-'+label)
command=['xcrun','swiftc','-swift-version','5','-target','arm64-apple-macosx13.0','-module-cache-path','/private/tmp/theora-native-feature-tests-cache','-parse-as-library','-D','FERAL_NATIVE_SOCKET_FIXTURE',*files,'-o',str(binary)]
compiled_result=subprocess.run(command,text=True,capture_output=True,timeout=180)
(tmp/('compile-'+label+'.txt')).write_text(compiled_result.stdout+compiled_result.stderr)
if compiled_result.returncode:
    print(compiled_result.stderr[-7000:]);sys.exit(compiled_result.returncode)
result=subprocess.run([str(binary)],text=True,capture_output=True,timeout=75)
(tmp/('result-'+label+'.txt')).write_text(result.stdout+result.stderr)
print(result.stdout[-3500:]);print(result.stderr[-1000:]);sys.exit(result.returncode)
