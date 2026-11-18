"""Worker-side projections and actions for the Wine runners page."""

from dataclasses import dataclass, replace
from contextlib import ExitStack
import os
from pathlib import Path
import shutil
import uuid

from packaging.version import Version as ProductVersion

from logos_desktop import artifacts, compatibility, config_store, constants, doctor, installer, maintenance, network
from logos_desktop import runtime_boundary, runtime_recipes
from logos_desktop import runtime_commands, runtime_copies, runtime_recovery, runtime_runners
from logos_desktop import runtime_stacks, runtime_storage, runtime_trials
from .bridge import Result


@dataclass(frozen=True)
class Version:
    runner: str
    family: str
    version: str
    digest: str
    size: int
    path: str
    generation: str
    current: bool
    blockers: tuple
    stack: str
    evidence: str
    affected: bool
    choices: tuple = ()
    setup_allowed: bool = False
    source: str = ""
    operation: str = ""
    transfer: str = ""
    result: str = ""
    reason: str = ""
    trial_space: str = ""
    trial_error: str = ""
    apply_error: str = ""
    stable_fallback: bool = False


@dataclass(frozen=True)
class RecipeChoice:
    stack: str
    label: str
    evidence: str
    selectable: bool
    experimental: bool
    details: str


@dataclass(frozen=True)
class RecoveryView:
    operation: str = ""
    point: str = ""
    phase: str = ""
    description: str = ""
    blocked: bool = False
    inspected: bool = False


@dataclass(frozen=True)
class Library:
    profile: str
    target: str
    rows: tuple
    space: str
    recovery: RecoveryView
    prerequisite: str = ""
    current_owned: bool = False
    install: str = ""
    profile_digest: str = ""
    retained: tuple = ()
    trial_choice: tuple = ()
    trial_reason: str = ""


def recovery_view(context):
    service = runtime_recovery.Recovery(context)
    if service.registered is None:
        return RecoveryView()
    record = service.status()
    phase = record["phase"]
    description = [f"Recovery: {phase} ({record['result']})."]
    if record["point"]:
        point = service._point_record(record["point"])
        description.append(f"Whole local installation point: {point['created']}. "
                           f"Retained point: {point['manifest']['logical_bytes']:,} logical bytes.")
        feedback = service.root / ("feedback-" + point["id"] + ".json")
        if os.path.lexists(feedback):
            try:
                local = runtime_recovery.read(feedback)
                if (local["point"], local["operation"], local["activation"], local["observation"]) == (
                        point["id"], record["id"], record["activation"], "works-for-me"):
                    description.append(f"Local observation on {local['recorded']}: the applied installation worked.")
            except (OSError, KeyError, ValueError, maintenance.MaintenanceError):
                description.append("The local observation could not be read.")
    if phase in ("returned", "resume-intent", "resumed"):
        description.append("Restored generation active. Newer local work retained separately.")
    elif phase == "applied":
        description.append("Applied generation active. The complete earlier installation is retained.")
    elif record["newer"]:
        description.append("Return is in progress. Preserve both generations.")
    if record["gate"] == "online-resume-pending":
        description.append("Inspect offline first. Online use needs separate confirmation.")
    return RecoveryView(record["id"], record["point"] or "", phase, " ".join(description),
                        phase not in ("resumed", "applied", "kept-current"), bool(record["inspection"]))


def library(app, snapshot, *, metadata_retry=False):
    context = maintenance.Context.from_app(app, prompt=False) if snapshot.selected else None
    try:
        recovery = recovery_view(context) if context else RecoveryView()
    except (OSError, ValueError, maintenance.MaintenanceError) as error:
        recovery = RecoveryView(description=f"Recovery status unavailable: {error}. Restore storage and recheck.",
                                blocked=True)
    try:
        return _library(app, snapshot, context, recovery, metadata_retry)
    except (OSError, ValueError, maintenance.MaintenanceError) as error:
        return Library(snapshot.profile, snapshot.version, (), f"Runner checks unavailable: {error}. "
                       "Restore the missing prerequisites and recheck status.", recovery)


def _library(app, snapshot, context, recovery, metadata_retry):
    catalog = runtime_stacks.bundled()
    store = runtime_runners.RunnerStore()
    copies = ()
    transfers = ()
    if os.path.lexists(store.root) or os.path.lexists(store.coordination):
        copies = tuple(copy for copy in store.inventory() if copy.state == "published")
        transfers = store.download_states()
        if copies:
            store.discover_profiles(app.conf.config_file_path if Path(app.conf.config_file_path).exists() else None)
    selected = doctor.stored(app.conf, "wine_appimage_path") or doctor.stored(app.conf, "wine_binary")
    active = config_store.absolute_path(selected, base=snapshot.path).resolve() if selected and snapshot.path else None
    setup = not snapshot.selected
    lookup = app.conf._network.product_release("Logos", "10", "stable", refresh=setup, force=setup and metadata_retry)
    stable = network.product_update_state("0.0", lookup)
    target = snapshot.version if snapshot.installed else stable.target if stable.actionable else ""

    rows = []
    host = {"architecture": os.uname().machine}
    observed = {}
    prerequisite = ""
    if context:
        try:
            fonts = context.prefix / "drive_c/windows/Fonts"
            host["font-arial"] = "present" if any(p.name.lower() == "arial.ttf" and p.is_file()
                                                  for p in fonts.iterdir()) else "unknown"
            observed = runtime_recipes.observe_prefix_settings(context.prefix)
            boundary = runtime_boundary.inspect((context.install, Path(snapshot.profile)), checkpoint=app.checkpoint)
            runtime_boundary.check_display(boundary, context.prefix, checkpoint=app.checkpoint)
        except (OSError, maintenance.MaintenanceError) as error:
            prerequisite = f"Trial and apply unavailable: {error}. Repair and recheck. Complete return stays available."
    for runner in catalog.runners.values():
        app.checkpoint()
        copy = next((item for item in copies if item.content_key == runner.content_key), None)
        stacks = [stack for stack in catalog.stacks.values() if stack.runner_id == runner.id
                  and runtime_stacks.same_product_build(target, stack.product_build)]
        wine = compatibility.parse_wine_version(dict(runner.companions)["wine"])
        choices = []
        for stack in stacks:
            recipe = catalog.recipes[stack.recipe_id]
            settings = dict(recipe.settings if setup else observed)
            if settings.get("windows-version") == "absent" and recipe.settings.get("windows-version") == "win10":
                settings["windows-version"] = "win10"
            assessment = runtime_stacks.assess(
                catalog, stack, product="Logos", product_build=target, host=host, settings=settings,
                dependencies={d.role: d.sha256 for d in recipe.dependencies},
                availability="cached" if copy else "unchecked")
            details = [f"Logos {stack.product_build}", *(f"{k}: {v}" for k, v in recipe.settings.items()),
                       *(f"{d.role}: {d.version}" for d in recipe.dependencies)]
            facts = [catalog.evidence[key] for key in stack.evidence_ids]
            details.extend(f"{fact.date}: {fact.reason}" for fact in facts)
            manual = (assessment.evidence == "experimental"
                      or assessment.evidence == "not-tested" and wine.channel.value != "stable")
            choices.append(RecipeChoice(stack.id, recipe.id, assessment.evidence, assessment.selectable, manual,
                                        "\n".join([assessment.reason, *details])))
        choice = choices[0] if choices else None
        affected = any(c.evidence == "known-affected" for c in choices)
        fallback = (bool(target) and setup and wine.channel.value == "stable" and not affected
                    and compatibility.wine_eligibility(wine, ProductVersion(target), "10")[0]
                    and not any(c.experimental for c in choices))
        evidence = choice.evidence.replace("-", " ").capitalize() if choice else "No matching project evidence"
        transfer = next((t for t in transfers if t[1] == runner.id and t[3] in ("pending", "unknown")), None)
        if not copy and not transfer and not any(t[1] == runner.id and t[3] == "succeeded" for t in transfers):
            transfer = next((t for t in transfers if t[1] == runner.id and t[3] in ("failed", "cancelled")), None)
        operation, _, phase, result, reason = transfer or ("", "", "", "", "")
        rows.append(Version(runner.id, runner.family, runner.version, runner.sha256, runner.size,
                            str(copy.path) if copy else "", copy.generation if copy else "",
                            bool(copy and active == copy.path), store.removal_reasons(runner.sha256) if copy else (),
                            choice.stack if choice else "", evidence, affected, tuple(choices),
                            fallback or any(c.evidence == "tested" for c in choices), runner.url,
                            operation, phase, result, reason, stable_fallback=fallback))
    space = ""
    trial_choice = ()
    trial_reason = snapshot.trial_status or "No trial recorded"
    if context and snapshot.installed and snapshot.state == snapshot.indexing == "Stopped":
        # This explicit page load may hash a large installation. Never run it on Home refresh.
        size = runtime_copies.manifest(context.prefix, checkpoint=app.checkpoint)
        if snapshot.trial_passed:
            try:
                proof = runtime_trials.successful_inputs(context, snapshot.profile, size)
                if proof["accepted"]["catalog"] == catalog.sha256:
                    trial_choice = (proof["generation"], proof["accepted"]["stack"])
                    trial_reason = "Trial settled and its same-copy restart was verified"
                else:
                    trial_reason = "The project catalog changed after the trial"
            except (OSError, maintenance.MaintenanceError) as error:
                trial_reason = str(error)
        needed = size.logical_bytes + size.entries * 4096 + runtime_copies.METADATA_RESERVE
        root = Path(constants.DATA_HOME)
        while not root.exists():
            root = root.parent
        copy_free = shutil.disk_usage(root).free
        return_free = shutil.disk_usage(context.prefix.parent).free
        space = (f"Measured installation: {size.logical_bytes:,} logical bytes. "
                 f"Allow at least {needed:,} bytes per independent copy, plus runtime files. "
                 f"Copy storage has {copy_free:,} bytes available. Return storage has {return_free:,} bytes available.")
        recovery_root = runtime_recovery.Recovery(context).root if recovery.operation else root
        recovery_free = shutil.disk_usage(recovery_root).free
        space += (f" A new point and an old-runtime inspection need at least {2 * needed:,} bytes "
                  f"on recovery storage ({recovery_free:,} available), plus runtime files. Space is checked again.")
        measured_rows = []
        for row in rows:
            if not row.path:
                measured_rows.append(row)
                continue
            try:
                extraction = runtime_boundary.expansion_budget(row.path, checkpoint=app.checkpoint)
                trial_needed = needed + extraction["bytes"] + 512 * 1024 * 1024
                trial_space = f"Trial copy and private runtime: {trial_needed:,} bytes required."
                trial_error = "Trial storage is too small. Free space and recheck." if trial_needed > copy_free else ""
            except (OSError, maintenance.MaintenanceError, artifacts.ArtifactError) as error:
                trial_space = ""
                trial_error = f"Trial space could not be measured: {error}"
            apply_error = ("Insufficient recovery or return space. Free space and recheck."
                           if recovery_free < 2 * needed or return_free < needed else "")
            measured_rows.append(replace(row, trial_space=trial_space, trial_error=trial_error,
                                         apply_error=apply_error))
        rows = measured_rows
        if recovery.phase in ("returned", "resumed", "resume-intent"):
            service = runtime_recovery.Recovery(context)
            journal = service.status()
            newer = Path(journal["newer"])
            service._moved_identity(journal["newer_identity"], newer)
            retained = runtime_copies.manifest(newer, logical_root=context.prefix, checkpoint=app.checkpoint)
            space += (f" Retained newer work: {retained.logical_bytes:,} logical bytes, "
                      f"{retained.allocated_bytes:,} allocated bytes. Shared extents may use less disk.")
    retained = runtime_storage.retained(context, checkpoint=app.checkpoint) if context else ()
    profile_path = Path(snapshot.profile)
    return Library(snapshot.profile, target or "Not available", tuple(rows), space, recovery, prerequisite,
                   any(r.current for r in rows), str(context.install) if context else "",
                   artifacts.regular_hash(profile_path) if profile_path.is_file() else "", retained,
                   trial_choice, trial_reason)


def setup_runtime(app, cancel, draft, snapshot):
    """Resolve a setup choice against the actual stable target before any configuration write."""
    recommended = draft.runtime == constants.WINE_RECOMMENDED_SIGIL
    store = runtime_runners.RunnerStore()
    path = Path(draft.runtime)
    if not recommended and not (path.is_absolute() and path.resolve().is_relative_to(store.root)):
        if path.suffix.lower() == ".appimage" and path.is_file():
            digest = artifacts.regular_hash(path.resolve(strict=True), checkpoint=app.checkpoint)
            failure = runtime_stacks.affected(runtime_stacks.bundled(), digest, draft.release,
                                             {"architecture": os.uname().machine}, {}, {})
            if failure:
                raise maintenance.RecoveryRequired("This Wine build is known affected: " + failure.reason)
        return draft, None
    view = library(app, snapshot)
    if view.target != draft.release:
        raise maintenance.MaintenanceError("Stable Logos metadata changed or is unavailable. Recheck before setup.")
    options = [r for r in view.rows if r.setup_allowed and not r.affected and (recommended or str(path) == r.path)]
    if not options:
        raise maintenance.RecoveryRequired("No runtime recommendation available. Recheck Wine runners before setup.")
    catalog = runtime_stacks.bundled()

    def tested(row):
        if draft.generation and not draft.stack:
            return []
        return [c for c in row.choices if c.evidence == "tested" and (not draft.stack or c.stack == draft.stack)]

    options = [row for row in options if tested(row) or (not draft.stack and row.stable_fallback)]
    if not options:
        raise maintenance.RecoveryRequired("The selected settings are unavailable for setup. Choose another option.")

    def rank(row):
        output = dict(catalog.runners[row.runner].companions)["wine"]
        return bool(tested(row)), compatibility.parse_wine_version(output).version

    runner = max(options, key=rank)
    wanted = tested(runner)
    choice = wanted[0].stack if wanted else None
    if len(wanted) > 1:
        choice = app.ask("Choose a tested settings recipe for this installation.", [c.stack for c in wanted])
    if app.conf._overrides.assume_yes:
        raise maintenance.MaintenanceError("Review this runtime choice without --assume-yes.")
    if not runner.path:
        app.approve_or_exit(f"Download Wine {runner.version} for setup review?", f"{runner.size:,} bytes.")
        store.download(catalog.runners[runner.runner], cancel=cancel,
                       status=lambda operation, message, fraction: app.status(message, fraction))
    final = library(app, snapshot)
    runner = next((r for r in final.rows if r.runner == runner.runner and r.path and r.setup_allowed), None)
    if final.target != draft.release or runner is None or runner.affected:
        raise maintenance.RecoveryRequired("Setup target or download changed. Review the verified download.")
    if choice and not any(c.stack == choice and c.evidence == "tested" and c.selectable for c in runner.choices):
        raise maintenance.RecoveryRequired("Setup evidence or availability changed. Review the verified download.")
    if not choice and not runner.stable_fallback:
        raise maintenance.RecoveryRequired("Stable fallback eligibility changed. Review the verified download.")
    summary = ("Project-tested settings: " + choice if choice else
               "No matching tested settings. Use this eligible stable runner as an unqualified choice.")
    app.approve_or_exit(f"Use Wine {runner.version} for new Logos {draft.release}?", summary)
    copies = [copy for copy in store.inventory() if copy.state == "published" and copy.sha256 == runner.digest]
    if len(copies) != 1:
        raise maintenance.MaintenanceError("The verified setup download changed. Reopen Wine runners.")
    copy = copies[0]
    if draft.generation and draft.generation != copy.generation:
        raise maintenance.RecoveryRequired("The setup download changed. Choose the version again.")
    catalog = runtime_stacks.bundled()
    artifact = artifacts.AcceptedArtifact(copy.path, catalog.runners[runner.runner].spec(), copy.sha256,
                                          copy.size, "verified setup generation")
    prepared = None
    if choice:
        _, generation, prepared = runtime_commands.managed_candidate(
            catalog, choice, checkpoint=app.checkpoint)
        if generation != copy.generation:
            raise maintenance.RecoveryRequired("The managed setup stack changed. Recheck before installation.")
    setup = installer.ManagedSetup(copy.generation, artifact, draft.release, prepared,
                                   (("architecture", os.uname().machine),))
    return replace(draft, runtime=str(copy.path)), setup


def inspect_offline(app, recovery, cancel, *, newer=False):
    with ExitStack() as prompts:
        pending = key = None
        requested = ""

        def progress(record):
            nonlocal pending, key, requested
            worker = record["worker"]
            if cancel.is_set() or not worker:
                prompts.close()
                return
            unknown = worker["phase"] == "unknown" or record["result"] == "unknown"
            state = (worker["phase"], record["restarts"], worker["reason"], record["result"], record["reason"])
            if state != key:
                prompts.close()
                pending = None
                key = state
                if unknown:
                    reason = worker["reason"] or record["reason"]
                    message = f"Still running or unknown: {reason}. Keep the copy and its recovery point."
                    choices = ["Retry normal shutdown", "Recheck status"]
                elif worker["phase"] == "running" and requested != "stop":
                    message = ("Open Library, a resource and a document. Then restart this offline copy."
                               if not record["restarts"] else "Reopen the document after restart, then end inspection.")
                    if newer:
                        message += r" To keep text, export it in Logos to C:\M4Export as a .txt file."
                    choices = ["End inspection"] if record["restarts"] else ["Restart copy", "End inspection"]
                else:
                    return
                pending = prompts.enter_context(app.pending_prompt(message, choices))
            if pending is None or not pending.done.is_set():
                return
            choice = pending.answer
            prompts.close()
            pending = None
            helper = "inspection-" + record["id"]
            if choice == "Restart copy":
                recovery.restart_helper(helper)
                requested = "restart"
            elif choice == "Recheck status":
                key = None
            else:
                recovery.stop_helper(helper)
                requested = "stop"
                if unknown:
                    key = None

        def observe():
            prompts.close()
            return {key: app.approve(f"Did you verify {key} in this offline copy?")
                    for key in ("library", "resource", "documents", "restart")}

        record = recovery.inspect_generation(newer=newer, observe=observe, status=progress, stop=cancel)
        if newer:
            export_dir = recovery.root / ("inspection-" + record["id"]) / "prefix/drive_c/M4Export"
            names = sorted(path.name for path in export_dir.glob("*.txt") if path.is_file())
            if names and app.approve("Copy an exported text document out of this offline inspection?"):
                name = app.ask("Choose the document you exported.", names)
                folder = app.ask("Choose a separate destination folder. Existing files are preserved.",
                                 [constants.PROMPT_OPTION_DIRECTORY])
                recovery.export_file(record["id"], name, Path(folder) / name)
        return record


def act(app, cancel, action, row, view, retries, *, import_path=None):
    if view.profile != app.conf.config_file_path:
        raise maintenance.MaintenanceError("The profile changed. Reopen Wine runners.")
    if app.conf._overrides.assume_yes and action not in ("download", "retry"):
        raise maintenance.MaintenanceError("Reopen without --assume-yes to review this runtime change.")
    store = runtime_runners.RunnerStore()
    catalog = runtime_stacks.bundled()
    app.checkpoint()
    if action == "import":
        if import_path is None:
            raise maintenance.MaintenanceError("Choose a local Wine AppImage to import")
        operation = store.import_file(Path(import_path), catalog, cancel=cancel)
        return Result(store.operation(operation)["result"], "Imported verified bytes. Your selection is unchanged.")
    if action in ("download", "retry"):
        runner = catalog.runners[row.runner]
        operation = str(uuid.uuid4())
        key = (view.profile, runner.id)

        def progress(_operation, message, fraction):
            app.status(f"{runner.version}: {message}", fraction)

        try:
            if action == "retry":
                store.retry(retries.get(key) or row.operation, runner,
                            new_operation_id=operation, cancel=cancel, status=progress)
            else:
                store.download(runner, operation_id=operation, cancel=cancel, status=progress)
        except Exception:
            try:
                record = store.operation(operation)
            except (KeyError, FileNotFoundError):
                raise
            if record["result"] in ("failed", "cancelled"):
                retries[key] = operation
            if record["result"] == "cancelled":
                return Result("cancelled", "Download cancelled. The current Wine selection is unchanged.")
            raise
        retries.pop(key, None)
        return Result("succeeded", f"Downloaded {runner.version}. The current Wine selection is unchanged.")
    if action == "cancel_transfer":
        record = store.operation(row.operation)
        if record["runner"] != row.runner:
            raise maintenance.MaintenanceError("Download identity changed. Recheck status.")
        result = store.cancel_download(row.operation)
        return Result("succeeded", f"Download: {result}. Recheck status before retrying.")
    if action == "remove":
        selected = Path(app.conf.config_file_path)
        store.discover_profiles(selected if selected.exists() or view.install else None)
        reasons = store.removal_reasons(row.digest)
        if reasons:
            raise maintenance.RecoveryRequired("Remove download blocked: " + ", ".join(reasons))
        app.approve_or_exit(f"Remove the unused {row.version} download?", f"Remove {row.size:,} bytes.")
        app.checkpoint()
        store.remove(row.generation)
        return Result("succeeded", "Removed the unused download.")

    context = maintenance.Context.from_app(app, prompt=False)
    if (view.install and str(context.install) != view.install
            or view.profile_digest and artifacts.regular_hash(view.profile) != view.profile_digest):
        raise maintenance.RecoveryRequired("The selected installation changed. Recheck its status before continuing.")
    if action in ("try", "apply", "skip"):
        if not row.stack or row.affected:
            raise maintenance.RecoveryRequired("No eligible stack for this Logos build")
        if action == "try":
            app.approve_or_exit("Start an offline trial?", "Close Logos and its indexer. "
                                "The original stays reserved while you use the temporary copy.")
            def progress(phase, measured):
                message = "Offline trial: " + phase.replace("-", " ")
                if isinstance(measured, dict) and measured.get("total"):
                    message += f" ({measured['bytes']:,} of {measured['total']:,} bytes)"
                app.status(message)

            report = runtime_commands.start_trial(context, app.conf, catalog, row.stack,
                                                  owner=app._instance_session, cancel=cancel, checkpoint=app.checkpoint,
                                                  progress=progress)
            outcome = report["result"] if report["phase"] in ("ended", "conflict") else "succeeded"
            return Result(outcome, f"Trial: {report['phase']} ({report['reason']}).")
        skip = action == "skip"
        trial = runtime_trials.startup_status(context.prefix) if skip else None
        reason = (f"Latest trial: {trial['phase']} ({trial['result']}, {trial['reason']})." if trial else
                  "No successful trial is being used for this change.")
        message = (reason + " You chose to skip the trial checklist. " if skip else "")
        experimental = any(c.experimental for c in row.choices if c.stack == row.stack)
        if experimental:
            app.approve_or_exit("Apply this experimental candidate?", "Project testing is incomplete.")
        message += "Verify a whole-installation recovery point and inspect the old version offline first."
        runtime_commands.apply_stack(
            context, app.conf, catalog, row.stack,
            lambda question: "yes" if app.approve(question.partition("?")[0] + "?") else "no", skip,
            owner=app, inspect=lambda recovery: inspect_offline(app, recovery, cancel),
            acknowledge=lambda: app.approve_or_exit(f"Apply {row.version} to the original installation?", message),
            experimental=experimental)
        return Result("succeeded", "Wine applied. Your complete recovery point is retained.")

    service = runtime_recovery.Recovery(context)
    record = service.status()
    expected = view.recovery
    if (record["id"], record["point"] or "", record["phase"]) != (expected.operation, expected.point, expected.phase):
        raise maintenance.RecoveryRequired("The active recovery changed. Recheck its status.")
    if action == "keep":
        app.approve_or_exit("Keep the current installation and leave apply?")
        service.keep_current(expected_revision=record["revision"])
    elif action == "feedback":
        service.record_local_feedback(expected.point)
        return Result("succeeded", "Saved your local observation. Recovery and project evidence are unchanged.")
    elif action in ("inspect", "newer"):
        inspect_offline(app, service, cancel, newer=action == "newer")
    elif action == "return":
        confirmation = service.return_confirmation()
        app.approve_or_exit(confirmation["message"])
        app.checkpoint()
        if record["phase"] in runtime_recovery.RETURN_PHASES:
            service.reconcile_return(confirmation)
        else:
            service.return_prefix(confirmation)
        service.rebind_restored(app)
    elif action == "resume":
        service.rebind_restored(app)
        confirmation = service.resume_confirmation()
        app.approve_or_exit(confirmation["message"])
        app.checkpoint()
        service.resume_online(confirmation, retained_work_acknowledged=True)
    else:
        raise ValueError("Unknown runtime action")
    return Result("succeeded", "Recovery action completed. Recheck the active generation.")
