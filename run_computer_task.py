#!/usr/bin/env python3
"""Run a bounded named computer task with task-scoped MCP app approvals."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

# The installed entry may be a symlink; sibling protocol modules stay with the skill.
sys.path.insert(0, str(Path(__file__).parent))
from agent_profiles import catalog_path, default_codex_home, home_config, load_binding, read_toml, resolve_catalog
from preflight_dispatch import PROFILE_KEYS


def load_task(path: Path) -> dict:
    task = json.loads(path.read_text())
    if not isinstance(task, dict) or set(task) != {'task', 'authorization', 'apps'}:
        raise ValueError('task file requires only task, authorization, apps')
    for key in ('task', 'authorization'):
        if not isinstance(task[key], str) or not task[key].strip():
            raise ValueError(f'{key} must be nonempty; authorization must quote the actual user scope')
    if not isinstance(task['apps'], dict) or not task['apps']:
        raise ValueError('apps must map exact app IDs to authorized UI tool names')
    for app, names in task['apps'].items():
        if not isinstance(app, str) or not app or not isinstance(names, list) or not names:
            raise ValueError('invalid app grant')
        if any(not isinstance(n, str) or not n for n in names):
            raise ValueError('tool names must be nonempty strings')
    return task


def observe(home: Path, thread_id: str, instructions: str) -> dict:
    # Only read the exact thread's runtime record; never scan credentials or all histories.
    import re
    if not re.fullmatch(r'[0-9a-f-]{36}', thread_id):
        raise ValueError('invalid thread ID')
    for path in (home / 'sessions').glob(f'*/*/*/rollout-*{thread_id}.jsonl'):
        result = {"role_instructions_present": False}
        with path.open() as stream:
            for line in stream:
                e = json.loads(line); p = e.get('payload', {})
                if e.get('type') == 'session_meta':
                    source = p.get('source', {})
                    spawn = source.get('subagent', {}).get('thread_spawn', {}) if isinstance(source, dict) else {}
                    result.update(agent_name=spawn.get('agent_role'), parent=spawn.get('parent_thread_id'),
                                  model_provider=p.get('model_provider'))
                if e.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') == 'developer':
                    text = '\n'.join(x.get('text', '') for x in p.get('content', []))
                    result['role_instructions_present'] |= instructions.strip() in text
                if e.get('type') == 'turn_context':
                    result.update(model=p.get('model'), model_reasoning_effort=p.get('effort'),
                                  sandbox_mode=p.get('sandbox_policy', {}).get('type'), turn_id=p.get('turn_id'))
        return result
    raise ValueError('runtime record unavailable')


def verify_runtime(home: Path, thread_id: str, binding: dict, parent: str | None) -> dict:
    actual = observe(home, thread_id, binding['developer_instructions'])
    if not actual['role_instructions_present']:
        raise ValueError('role instructions changed or are not loaded; start a new named task')
    keys = ('agent_name', 'model', 'model_reasoning_effort', 'sandbox_mode')
    if any(actual.get(k) != binding[k] for k in keys):
        raise ValueError('named role/model/effort/permission mismatch')
    if parent is not None and actual.get('parent') != parent:
        raise ValueError('request is not from the assigned child')
    if binding.get('model_provider') and actual.get('model_provider') != binding['model_provider']:
        raise ValueError('provider mismatch')
    return actual


def app_approval(params: dict, task: dict) -> tuple[bool, dict]:
    meta = params.get('_meta') or {}
    if not isinstance(meta, dict): return False, {'reason': 'invalid approval metadata'}
    tool_params = meta.get('tool_params') or {}
    if not isinstance(tool_params, dict): return False, {'reason': 'invalid app target'}
    app = tool_params.get('app')
    brief = {'app': app, 'tool': meta.get('tool_name'), 'mode': params.get('mode')}
    # This accepts only the app-access form, not action confirmations or device proofs.
    allowed = (
        params.get('serverName') == 'cua_repl' and params.get('mode') == 'form'
        and params.get('requestedSchema') == {'type': 'object', 'properties': {}}
        and meta.get('codex_approval_kind') == 'mcp_tool_call'
        and meta.get('connector_id') == 'computer-use' and meta.get('riskLevel') == 'low'
        and 'codex_request_type' not in meta
        and meta.get('tool_params') == {'app': app}
        and isinstance(app, str) and app in task['apps']
        and meta.get('tool_name') in task['apps'][app]
    )
    return allowed, brief


def run(args: argparse.Namespace, task: dict, binding: dict) -> dict:
    output = args.output_dir.resolve()
    volume = Path('/Volumes/工作盘')
    if not volume.is_mount() or not output.is_relative_to(volume.resolve()):
        raise ValueError('output must resolve onto mounted /Volumes/工作盘')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'task.json').write_text(json.dumps(task, ensure_ascii=False, indent=2))
    if args.resume_thread:
        verify_runtime(args.codex_home, args.resume_thread, binding, None)
    env = os.environ.copy()
    env.update(CODEX_HOME=str(args.codex_home), TMPDIR=str(output), PYTHONDONTWRITEBYTECODE='1')
    command = [args.cli, 'app-server', '-c', 'skills.include_instructions=false', '-c', 'features.memories=false']
    if binding.get('model_provider'):
        command += ['-c', 'model_provider=' + json.dumps(binding['model_provider'])]
    result = {'status': 'failed', 'role': binding['agent_name'], 'binding_verified': False,
              'accepted_app_requests': 0, 'evidence_dir': str(output), 'main_verification_required': True}
    active = {}; parent = None; child = args.resume_thread; root_thread = None
    final_text = {}; observed = None
    with (output / 'stderr.log').open('w') as err, (output / 'events.jsonl').open('w') as events:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                                text=True, bufsize=1, env=env, cwd=output)
        messages = queue.Queue()
        def reader():
            for line in proc.stdout:
                messages.put(line)
            messages.put(None)
        receiver = threading.Thread(target=reader, daemon=True)
        receiver.start()
        def send(message):
            proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n'); proc.stdin.flush()
        def audit(event):
            events.write(json.dumps(event, ensure_ascii=False) + '\n'); events.flush()
        prompt = (f"Task: {task['task']}\nUser authorization: {task['authorization']}\n"
                  f"Allowed app-access tools: {json.dumps(task['apps'], ensure_ascii=False)}\n"
                  f"Evidence directory: {output}\n"
                  'Use CUA only for UI actions, verify the final visible state, and return concise evidence and gaps. '
                  'Do not broaden the authorization, change access settings, persist approvals, or delegate further.')
        try:
            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'delegated_computer_task', 'version': '2026.10.30.1'},
                'capabilities': {'experimentalApi': True, 'mcpServerOpenaiFormElicitation': True}}})
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                try: line = messages.get(timeout=min(1, max(0.01, deadline-time.monotonic())))
                except queue.Empty: continue
                if line is None: raise RuntimeError('app-server exited before completion')
                msg = json.loads(line); method = msg.get('method', ''); params = msg.get('params', {})
                if 'error' in msg: raise RuntimeError(str(msg['error']))
                if msg.get('id') == 1 and 'result' in msg:
                    send({'method': 'initialized', 'params': {}})
                    common = {'sandbox': binding['sandbox_mode'], 'approvalPolicy': 'on-request'}
                    if child:
                        send({'id': 2, 'method': 'thread/resume', 'params': {
                            **common, 'threadId': child, 'excludeTurns': True}})
                    else:
                        send({'id': 2, 'method': 'thread/start', 'params': {
                            **common, 'model': binding['model'], 'cwd': str(output),
                            'config': {'model_reasoning_effort': binding['model_reasoning_effort']},
                            'developerInstructions': 'Coordinate this single authorized named-agent task; do not perform its UI actions yourself.'}})
                elif msg.get('id') == 2 and 'result' in msg:
                    root_thread = msg['result']['thread']['id']
                    if child:
                        turn_prompt = prompt
                    else:
                        parent = root_thread
                        turn_prompt = (f'Spawn exactly one native {binding["agent_name"]} using agent_type, '
                            'fork_context=false, no model/effort override. Give it the task below. '
                            'Wait for it, then close it. Do not use a fallback role or do its task yourself.\n' + prompt)
                    send({'id': 3, 'method': 'turn/start', 'params': {
                        'threadId': root_thread, 'input': [{'type': 'text', 'text': turn_prompt}]}})
                elif 'id' in msg and method:
                    if method != 'mcpServer/elicitation/request':
                        send({'id': msg['id'], 'error': {'code': -32601, 'message': 'No authorization for this request'}})
                        result.update(status='approval_required', reason='unsupported server request: '+method); break
                    request_thread = params.get('threadId')
                    allowed, brief = app_approval(params, task)
                    if allowed:
                        allowed = request_thread != parent and (child is None or request_thread == child)
                    if allowed:
                        observed = verify_runtime(args.codex_home, request_thread, binding, parent)
                        allowed = bool(params.get('turnId')) and observed.get('turn_id') == params['turnId']
                        active[request_thread] = params['turnId']
                    audit({'event': 'app_approval', **brief, 'accepted': allowed})
                    send({'id': msg['id'], 'result': {'action': 'accept' if allowed else 'decline',
                                                     'content': {} if allowed else None}})
                    if not allowed:
                        result.update(status='approval_required', request=brief,
                                      reason='request exceeds current task grant; no retry or persistent grant'); break
                    child = request_thread
                    result['accepted_app_requests'] += 1
                if method == 'turn/started':
                    active[params['threadId']] = params['turn']['id']
                elif method == 'item/completed':
                    item = params.get('item', {})
                    if item.get('type') == 'collabAgentToolCall' and item.get('tool') == 'spawnAgent' and item.get('status') == 'completed':
                        ids = item.get('receiverThreadIds', [])
                        if item.get('senderThreadId') != parent or len(ids) != 1 or (child and child != ids[0]):
                            raise RuntimeError('unexpected or additional child assignment')
                        child = ids[0]
                    if item.get('type') == 'agentMessage':
                        final_text[params.get('threadId')] = item.get('text', '')
                    # Full local evidence; only the compact result is returned to the caller.
                    audit(msg)
                elif method == 'turn/completed':
                    audit(msg)
                    if params.get('turn', {}).get('status') == 'failed':
                        result.update(status='failed', reason=params['turn'].get('error')); break
                    if params.get('threadId') == root_thread:
                        if not child: raise RuntimeError('no verified named child completion; inspect local events')
                        observed = verify_runtime(args.codex_home, child, binding, parent)
                        result.update(status='turn_completed', binding_verified=True, observed_binding=observed,
                                      resume_thread=child, result=final_text.get(child, final_text.get(root_thread, '')))
                        break
            else:
                result.update(status='timeout', reason='task deadline reached; no automatic retry')
        except (ValueError, RuntimeError, OSError) as exc:
            result.update(status='failed', reason=str(exc))
        finally:
            # Unfinished work must not keep acting after a refusal or client exit.
            if result['status'] != 'turn_completed':
                for thread_id, turn_id in active.items():
                    try: send({'id': 'stop-'+thread_id, 'method': 'turn/interrupt',
                               'params': {'threadId': thread_id, 'turnId': turn_id}})
                    except (OSError, ValueError): pass  # Process termination below is the fallback.
            try: proc.stdin.close()
            except OSError: pass
            proc.terminate()
            try: proc.wait(timeout=8)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            receiver.join(timeout=1)
            proc.stdout.close()
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--task', type=Path, required=True, help='JSON: task, exact user authorization, app IDs -> tool names')
    p.add_argument('--output-dir', type=Path, required=True, help='New directory on /Volumes/工作盘')
    p.add_argument('--resume-thread', help='Verified same-role child from an earlier result; never a main/user chat')
    p.add_argument('--role', choices=['delegated_computer_worker', 'delegated_senior_worker'], default='delegated_computer_worker')
    p.add_argument('--codex-home', type=Path, default=default_codex_home())
    p.add_argument('--cli', default='codex')
    p.add_argument('--timeout', type=int, default=300)
    a = p.parse_args()
    if not 1 <= a.timeout <= 1800: p.error('timeout must be 1..1800 seconds')
    try:
        a.codex_home = a.codex_home.resolve()
        task = load_task(a.task); binding = load_binding(a.codex_home, a.role)
        profile = read_toml(Path(binding['config_file']))
        if set(profile) - PROFILE_KEYS: raise ValueError('unsupported role fields; cannot partially apply role')
        binding['developer_instructions'] = profile['developer_instructions']
        provider = profile.get('model_provider', home_config(a.codex_home).get('model_provider'))
        if provider: binding['model_provider'] = provider
        resolve_catalog(catalog_path(a.codex_home), binding['model'], binding['model_reasoning_effort'])
        result = run(a, task, binding)
    except (ValueError, OSError, SystemExit) as exc:
        result = {'status': 'failed', 'reason': str(exc)}
    brief = {k: v for k, v in result.items() if k not in ('result', 'observed_binding')}
    if 'result' in result:
        brief['result_excerpt'] = result['result'][:1200]
        brief['result_truncated'] = len(result['result']) > 1200
    print(json.dumps(brief, ensure_ascii=False))
    raise SystemExit(0 if result['status'] == 'turn_completed' else 2)


if __name__ == '__main__': main()
