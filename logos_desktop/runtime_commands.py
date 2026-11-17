"""Selected-profile runtime controls for headless dispatch."""

import json
import os
import select
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from . import artifacts, constants, instance, maintenance, runtime_boundary, runtime_recipes, runtime_recovery
from . import runtime_runners, runtime_stacks, runtime_trials


def trial_report(record):
    phase = record['phase']
    reason = record['reason']
    known = ('owned-inspection', 'verifying-source', 'supervisor-unavailable', 'source-changed',
             'supervision-interrupted', 'preparation-failed', 'namespace-exit-unknown', 'processes-settled',
             'shutdown-timeout', 'cancelled-before-launch', 'display-lost', 'worker-result-unaccepted')
    actions = [] if phase == 'ended' else ['status', 'stop']
    if phase == 'conflict':
        actions = ['inspect-reservation'] if reason == 'supervisor-unavailable' else ['inspect-reservation', 'stop']
    return {'operation': record['id'], 'phase': phase, 'result': record['result'],
            'reason': reason if reason in known else 'inspect-operation', 'next_actions': actions}


def arguments(group, options):
    for flag, help_text in (
        ('stacks', 'list bundled runtime stacks'), ('runners', 'list verified managed runner copies'),
    ):
        group.add_argument('--runtime-' + flag, action='store_true', help=help_text)
    for flag, argument in (
        ('runner-details', 'RUNNER_ID'), ('download', 'RUNNER_ID'), ('download-retry', 'OPERATION_ID'),
        ('download-cancel', 'OPERATION_ID'), ('remove-download', 'RUNNER_ID'), ('import', 'PATH'),
        ('trial', 'STACK_ID'), ('trial-stop', 'OPERATION_ID'), ('trial-restart', 'OPERATION_ID'),
        ('apply', 'STACK_ID'), ('inspect', 'RECOVERY_ID'), ('return', 'RECOVERY_ID'),
        ('resume-online', 'RECOVERY_ID'), ('remove-recovery', 'RECOVERY_ID'), ('keep-current', 'OPERATION_ID'),
    ):
        group.add_argument('--runtime-' + flag, metavar=argument)
    for flag in ('trial-status', 'recovery-status'):
        group.add_argument('--runtime-' + flag, nargs='?', const=True, metavar='ID')
    options.add_argument('--json', action='store_true', help='emit structured runtime status')
    options.add_argument('--without-successful-trial', action='store_true', help='acknowledge a trial did not pass')


def selected(args):
    return next((key.removeprefix('runtime_') for key, value in vars(args).items()
                 if key.startswith('runtime_') and value), None)


def managed_candidate(catalog, stack_id):
    store = runtime_runners.RunnerStore()
    runner = catalog.runners[catalog.stacks[stack_id].runner_id]
    copies = [item for item in store.inventory()
              if item.state == 'published' and item.content_key == runner.content_key]
    if len(copies) != 1:
        raise maintenance.MaintenanceError(f'Download one verified copy with --runtime-download {runner.id}')
    selected = copies[0]
    root = maintenance.private_directory(Path(constants.DATA_HOME) / 'prepared-stacks')
    prepared = runtime_recipes.prepare(catalog, stack_id, root, candidates=[selected.path])
    accepted = artifacts.AcceptedArtifact(selected.path, runner.spec(), selected.sha256, selected.size,
                                          'verified managed copy')
    return store, selected.generation, replace(prepared, runner=accepted)


def start_trial(context, conf, catalog, stack_id):
    from . import utils

    with instance.Session.for_config(conf._overrides) as session:
        executable = utils.find_installed_product(context.product, str(context.prefix))
        if executable is None:
            raise maintenance.MaintenanceError('The selected installation has no local Logos executable')
        relative = Path(executable).resolve(strict=True).relative_to(context.prefix / 'drive_c')
        store, generation, prepared = managed_candidate(catalog, stack_id)
        parent = maintenance.private_directory(Path(constants.DATA_HOME) / 'runtime-trials')
        task = runtime_trials.start(context, prepared, parent=parent, store=store, generation=generation,
                                    executable=str(PureWindowsPath('C:/', *relative.parts)),
                                    profile=conf.config_file_path, session=session)
        try:
            while True:
                record = task.snapshot()
                if record['running_acknowledged'] or record['phase'] in ('ended', 'conflict'):
                    return trial_report(record)
                time.sleep(.1)
        finally:
            task.close()


def inspect_point(recovery, point, confirm):
    if recovery.status()['point'] != point:
        raise maintenance.RecoveryRequired('Inspect the selected active recovery point')
    if not sys.stdin.isatty():
        raise maintenance.MaintenanceError('Offline inspection needs a terminal for actual observations')
    prompted = False
    restart_requested = False
    input_closed = False

    def progress(record):
        nonlocal prompted, restart_requested, input_closed
        if not input_closed and record['worker'] and record['worker']['phase'] in ('running', 'unknown'):
            if not prompted:
                print('Inspect the offline library, a resource and documents. Type restart or stop:', flush=True)
                prompted = True
            if not select.select([sys.stdin], [], [], 0)[0]:
                return
            line = sys.stdin.readline()
            if line.strip() == 'restart' and not restart_requested and record['worker']['phase'] == 'running':
                recovery.restart_helper('inspection-' + record['id'])
                restart_requested = True
                print('After reviewing the restarted copy, type stop to end inspection.', flush=True)
            elif not line or line.strip() == 'stop':
                recovery.stop_helper('inspection-' + record['id'])
                input_closed = not line

    def observe():
        return {key: confirm(f'Did you verify {key} in this offline copy? Type yes: ') == 'yes'
                for key in ('library', 'resource', 'documents', 'restart')}

    record = recovery.inspect_generation(observe=observe, status=progress)
    return {'inspection': record['id'], 'result': record['result']}


def apply_stack(context, conf, catalog, target, confirm, skip_trial):
    from . import config_store, utils
    from .cli import CLI

    with instance.Session.for_config(conf._overrides) as session:
        conf._overrides._instance_session = session
        app = CLI(conf._overrides)
        try:
            store, generation, prepared = managed_candidate(catalog, target)
            trial_id = None
            observations = None
            trial = runtime_trials.startup_status(context.prefix)
            if not skip_trial:
                if trial is None or trial['result'] != 'succeeded':
                    raise maintenance.RecoveryRequired('Run a successful trial or select --without-successful-trial')
                trial_id = trial['id']
                observations = {key: confirm(f'Did you verify {key} in trial {trial_id}? Type yes: ') == 'yes'
                                for key in ('library', 'search', 'typing', 'scrolling', 'restart')}
                if not all(observations.values()):
                    raise maintenance.RecoveryRequired('Trial checklist is incomplete')
            elif trial is None:
                print('No local trial is recorded for this installation.')
            else:
                summary = trial_report(trial)
                print(f"Latest trial: {summary['phase']}, {summary['result']} ({summary['reason']}). "
                      'Apply will proceed without relying on its checklist.')
            expected = f'apply {target} to {context.prefix}' + (' without successful trial' if skip_trial else '')
            prompt = f'Change the original installation after complete recovery verification? Type {expected!r}: '
            if confirm(prompt) != expected:
                raise maintenance.MaintenanceError('Apply was not confirmed')
            document, _ = config_store.read_document(conf.config_file_path)
            effective = config_store.apply_changes(app.conf._raw._as_dict(), conf._overrides.runtime_selection or {},
                                                  conf.config_file_path)
            exe = utils.find_installed_product(context.product, str(context.prefix))
            old_path = config_store.absolute_path(document['wine_binary'], base=context.install).resolve(strict=True)
            old = next((copy for copy in store.inventory() if copy.path == old_path), None)
            if old is None or exe is None:
                raise maintenance.RecoveryRequired('Apply requires a recoverable managed selection and installed Logos')
            runtime_boundary.inspect((context.install, Path(conf.config_file_path)))
            fonts = context.prefix / 'drive_c/windows/Fonts'
            arial = fonts.is_dir() and any(p.name.lower() == 'arial.ttf' and p.is_file() for p in fonts.iterdir())
            host = {'architecture': os.uname().machine, 'font-arial': 'present' if arial else 'unknown'}
            build = utils.get_current_logos_version(str(Path(exe).parent))
            settings = runtime_recipes.observe_prefix_settings(context.prefix)
            if (settings.get('windows-version') == 'absent'
                    and prepared.recipe.settings.get('windows-version') == 'win10'):
                settings['windows-version'] = 'win10'
            assessment = runtime_stacks.assess(catalog, prepared.stack, product=context.product, product_build=build,
                                              host=host, settings=settings, availability='cached',
                                              dependencies={d.dependency.role: d.artifact.sha256
                                                            for d in prepared.dependencies})
            if not assessment.selectable:
                raise maintenance.RecoveryRequired('Candidate apply refused: ' + assessment.reason)
            recovery = runtime_recovery.Recovery(context)
            if recovery.registered is None:
                root = maintenance.private_directory(Path(constants.DATA_HOME) / 'complete-recovery')
                recovery = runtime_recovery.Recovery.create(context, root / str(uuid.uuid4()))
            journal = recovery.status()
            if journal['phase'] in ('preparing', 'kept-current', 'applied', 'resumed'):
                point = recovery.capture(store=store, generation=old.generation, profile=conf.config_file_path,
                                         portable=context.install / constants.PORTABLE_CONFIG_NAME,
                                         links=[context.install / 'data/bin' / name for name in
                                                ('selected_wine.AppImage', *artifacts.COMPANIONS)],
                                         product_build=build, effective=effective)
            elif journal['phase'] == 'point-verified':
                point = journal['point']
            else:
                raise maintenance.RecoveryRequired('Inspect the pending recovery before applying')
            if recovery.status()['capability'] is None:
                inspect_point(recovery, point, confirm)
            recovery.apply(app, prepared, store=store, generation=generation, host=host,
                           trial_id=trial_id, observations=observations, without_successful_trial=skip_trial)
            return {'point': point, 'result': 'succeeded'}
        finally:
            while not app.close(timeout=.1):
                pass


def execute(action, target, conf, *, confirm=input, skip_trial=False):
    catalog = runtime_stacks.bundled()
    if action == 'stacks':
        return {'stacks': [{'id': item.id, 'runner': item.runner_id, 'recipe': item.recipe_id,
                            'product_build': item.product_build} for item in catalog.stacks.values()]}
    if action == 'runner_details':
        item = catalog.runners[target]
        return {'runner': item.id, 'version': item.version, 'family': item.family, 'sha256': item.sha256,
                'bytes': item.size, 'stacks': [s.id for s in catalog.stacks.values() if s.runner_id == item.id]}
    if action in ('runners', 'download', 'download_retry', 'download_cancel', 'remove_download', 'import'):
        store = runtime_runners.RunnerStore()
        if action == 'import':
            operation = store.import_file(Path(target).absolute(), catalog)
            return {'operation': operation, 'result': store.operation(operation)['result']}
        if action == 'runners':
            if not os.path.lexists(store.root) and not os.path.lexists(store.coordination):
                return {'copies': []}
            return {'copies': [{'generation': c.generation, 'sha256': c.sha256, 'state': c.state}
                               for c in store.inventory()]}
        if action == 'download_cancel':
            return {'operation': target, 'result': store.cancel_download(target)}
        if action == 'remove_download':
            store.discover_profiles(conf.config_file_path)
            runner = catalog.runners[target]
            copies = [c for c in store.inventory() if c.content_key == runner.content_key and c.state == 'published']
            if len(copies) != 1:
                raise maintenance.MaintenanceError('Select one verified managed download to remove')
            reasons = store.removal_reasons(runner.sha256)
            if reasons:
                raise maintenance.MaintenanceError('Remove download blocked: ' + ', '.join(reasons))
            expected = 'remove ' + copies[0].generation
            if confirm(f'Remove {target} ({copies[0].size} bytes)? Type {expected!r}: ') != expected:
                raise maintenance.MaintenanceError('Removal was not confirmed')
            store.remove(copies[0].generation)
            return {'generation': copies[0].generation, 'result': 'succeeded'}
        operation = str(uuid.uuid4())
        print(f'Download operation: {operation}', flush=True)
        if action == 'download':
            store.download(catalog.runners[target], operation_id=operation)
        else:
            runner = catalog.runners[store.operation(target)['runner']]
            store.retry(target, runner, new_operation_id=operation)
        return {'operation': operation, 'result': store.operation(operation)['result']}

    context = maintenance.Context.from_app(SimpleNamespace(conf=conf), prompt=False)
    if action == 'trial':
        return start_trial(context, conf, catalog, target)
    if action == 'apply':
        return apply_stack(context, conf, catalog, target, confirm, skip_trial)
    if action in ('trial_status', 'trial_stop', 'trial_restart'):
        record = runtime_trials.selected_operation(context, None if target is True else target,
                                                  action=action.removeprefix('trial_'))
        return trial_report(record)

    recovery = runtime_recovery.Recovery(context)
    if action == 'inspect':
        return inspect_point(recovery, target, confirm)
    record = recovery.status()
    if action == 'keep_current':
        if record['id'] != target or record['phase'] not in ('preparing', 'point-verified'):
            raise maintenance.RecoveryRequired('Select the active pre-apply operation')
        if confirm(f'Keep the original installation? Type keep {target}: ') != 'keep ' + target:
            raise maintenance.MaintenanceError('Keep current was not confirmed')
        recovery.keep_current(expected_revision=record['revision'])
        return {'operation': target, 'result': 'succeeded'}
    if action == 'recovery_status':
        if target is not True and target != record['point']:
            raise maintenance.RecoveryRequired('This recovery point is not the selected active point')
        result = {key: record[key] for key in ('id', 'point', 'phase', 'result', 'gate')}
        result['reason'] = 'online-resume-pending' if record['gate'] == 'online-resume-pending' else record['phase']
        result['next_actions'] = ['status']
        if record['phase'] in ('point-verified', 'applied', *runtime_recovery.RETURN_PHASES):
            result['next_actions'].append('return')
        return result
    if action == 'return':
        question = recovery.return_confirmation()
    elif action == 'resume_online':
        question = recovery.resume_confirmation()
    elif action == 'remove_recovery':
        question = recovery.point_removal_confirmation(target)
    else:
        raise ValueError('Unsupported runtime action')
    if question['point'] != target:
        raise maintenance.RecoveryRequired('Recovery point does not match the selected operation')
    expected = action.replace('_', '-') + ' ' + target
    if confirm(f"{question['message']}\nType {expected!r} to confirm: ") != expected:
        raise maintenance.MaintenanceError('Recovery action was not confirmed')
    if action == 'return' and record['phase'] in runtime_recovery.RETURN_PHASES:
        recovery.reconcile_return(question)
    elif action == 'return':
        recovery.return_prefix(question)
    elif action == 'resume_online':
        recovery.resume_online(question, retained_work_acknowledged=True)
    else:
        recovery.remove_point(target, question)
    return {'point': target, 'result': 'succeeded'}


def dispatch(action, args, conf):
    if args.assume_yes and action in ('apply', 'return', 'remove_download', 'remove_recovery', 'resume_online',
                                      'keep_current'):
        raise maintenance.MaintenanceError('Recovery actions require scoped confirmation without --assume-yes')
    if args.json and action not in ('stacks', 'runners', 'runner_details', 'trial_status', 'recovery_status'):
        raise maintenance.MaintenanceError('--json requires a read-only runtime command')
    if action in ('return', 'remove_recovery', 'resume_online', 'inspect', 'keep_current'):
        with instance.Session.for_config(conf._overrides):
            result = execute(action, getattr(args, 'runtime_' + action), conf)
    else:
        result = execute(action, getattr(args, 'runtime_' + action), conf, skip_trial=args.without_successful_trial)
    print(json.dumps({'schema': 1, **result}, indent=None if args.json else 2))
    return 0
