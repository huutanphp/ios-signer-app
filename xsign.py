# https://mypy.readthedocs.io/en/stable/runtime_troubles.html#using-classes-that-are-generic-in-stubs-but-not-at-runtime
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess, PIPE, Popen, TimeoutExpired
import tempfile
import shutil
from typing import Callable, Dict, Optional, NamedTuple, Set
import re
import os
from util import *
import time


def plist_buddy(args: str, plist: Path, check: bool = True, xml: bool = False):
    cmd = ["/usr/libexec/PlistBuddy"]
    if xml:
        cmd.append("-x")
    return decode_clean(
        run_process(
            *cmd,
            "-c",
            args,
            str(plist),
            check=check,
        ).stdout
    )


def codesign(identity: str, component: Path, entitlements: Optional[Path] = None):
    cmd = ["/usr/bin/codesign", "--continue", "-f", "--no-strict", "-s", identity]
    if entitlements:
        cmd.extend(["--entitlements", str(entitlements)])
    return run_process(*cmd, str(component))


def codesign_async(identity: str, component: Path, entitlements: Optional[Path] = None):
    cmd = ["/usr/bin/codesign", "--continue", "-f", "--no-strict", "-s", identity]
    if entitlements:
        cmd.extend(["--entitlements", str(entitlements)])
    return subprocess.Popen([*cmd, str(component)], stdout=PIPE, stderr=PIPE)


def codesign_dump_entitlements(component: Path):
    return decode_clean(
        run_process("/usr/bin/codesign", "--no-strict", "-d", "--entitlements", ":-", str(component)).stdout
    )


def binary_replace(pattern: str, f: Path):
    return run_process("perl", "-p", "-i", "-e", pattern, str(f))


def security_dump_prov(f: Path):
    return decode_clean(run_process("security", "cms", "-D", "-i", str(f)).stdout)


plist_base = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
</dict>
</plist>
"""

adhoc_options_plist = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>ad-hoc</string>
	<key>iCloudContainerEnvironment</key>
	<string>Production</string>
</dict>
</plist>
"""


def exec_retry(name: str, func: Callable[[], CompletedProcess[bytes]]):
    start_time = time.time()
    last_error: Optional[Exception] = None
    while time.time() - start_time < 90:
        try:
            return func()
        except Exception as e:
            if isinstance(e.__cause__, TimeoutExpired):
                last_error = e
                print(f"{name} timed out, retrying")
            else:
                raise e
    raise Exception(f"{name} timed out too many times") from last_error


def xcode_archive(project_dir: Path, scheme_name: str, archive: Path):
    # Xcode needs to be open to "cure" hanging issues
    open_xcode(project_dir)
    try:
        return exec_retry("xcode_archive", lambda: _xcode_archive(project_dir, scheme_name, archive))
    finally:
        kill_xcode(True)


def _xcode_archive(project_dir: Path, scheme_name: str, archive: Path):
    return run_process(
        "xcodebuild",
        "-allowProvisioningUpdates",
        "-project",
        str(project_dir.resolve()),
        "-scheme",
        scheme_name,
        "clean",
        "archive",
        "-archivePath",
        str(archive.resolve()),
        timeout=20,
    )


def xcode_export(project_dir: Path, archive: Path, export_dir: Path):
    # Xcode needs to be open to "cure" hanging issues
    open_xcode(project_dir)
    try:
        return exec_retry("xcode_export", lambda: _xcode_export(project_dir, archive, export_dir))
    finally:
        kill_xcode(True)


def _xcode_export(project_dir: Path, archive: Path, export_dir: Path):
    options_plist = export_dir.joinpath("options.plist")
    with open(options_plist, "w") as f:
        f.write(adhoc_options_plist)
    return run_process(
        "xcodebuild",
        "-allowProvisioningUpdates",
        "-project",
        str(project_dir.resolve()),
        "-exportArchive",
        "-archivePath",
        str(archive.resolve()),
        "-exportPath",
        str(export_dir.resolve()),
        "-exportOptionsPlist",
        str(options_plist.resolve()),
        timeout=20,
    )


def dump_prov_entitlements_plist(prov_file: Path, entitlements_plist: Path):
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        prov_plist = tmpdir.joinpath("prov.plist")
        with open(prov_plist, "w") as f:
            s = security_dump_prov(prov_file)
            f.write(s)
        with open(entitlements_plist, "w") as f:
            s = plist_buddy("Print :Entitlements", prov_plist, xml=True)
            f.write(s)


def popen_check(pipe: Popen[bytes]):
    if pipe.returncode != 0:
        data = {"message": f"{pipe.args} failed with status code {pipe.returncode}"}
        if pipe.stdout:
            data["stdout"] = decode_clean(pipe.stdout.read())
        if pipe.stderr:
            data["stderr"] = decode_clean(pipe.stderr.read())
        raise Exception(data)


class SignOpts(NamedTuple):
    app_dir: Path
    common_name: str
    team_id: str
    prov_file: Optional[Path]
    bundle_id: Optional[str]
    bundle_name: Optional[str]
    patch_debug: bool
    patch_all_devices: bool
    patch_file_sharing: bool
    encode_ids: bool
    patch_ids: bool
    force_original_id: bool


def sign(opts: SignOpts):
    main_app = next(opts.app_dir.glob("Payload/*.app"))
    main_info_plist = main_app.joinpath("Info.plist")
    old_main_bundle_id = plist_buddy("Print :CFBundleIdentifier", main_info_plist)
    is_distribution = "Distribution" in opts.common_name

    if opts.prov_file:
        if opts.bundle_id is None:
            print("Using original bundle id")
            main_bundle_id = old_main_bundle_id
        elif opts.bundle_id == "":
            print("Using provisioning profile's application id")
            with tempfile.TemporaryDirectory() as tmpdir_str:
                entitlements_plist = Path(tmpdir_str).joinpath("entitlements.plist")
                dump_prov_entitlements_plist(opts.prov_file, entitlements_plist)
                prov_app_id = plist_buddy("Print :application-identifier", entitlements_plist)
                main_bundle_id = prov_app_id[prov_app_id.find(".") + 1 :]
                if "*" in main_bundle_id:
                    print("Provisioning profile is wildcard, using original bundle id")
                    main_bundle_id = old_main_bundle_id
        else:
            print("Using custom bundle id")
            main_bundle_id = opts.bundle_id
    else:
        if opts.bundle_id:
            print("Using custom bundle id")
            main_bundle_id = opts.bundle_id
        elif opts.encode_ids:
            print("Using encoded original bundle id")
            seed = old_main_bundle_id + opts.team_id
            main_bundle_id = gen_id(old_main_bundle_id, seed)
        else:
            print("Using original bundle id")
            main_bundle_id = old_main_bundle_id

    if opts.bundle_name:
        print(f"Setting CFBundleDisplayName to {opts.bundle_name}")
        plist_buddy(f"Delete :CFBundleDisplayName", main_info_plist, check=False)
        plist_buddy(f"Add :CFBundleDisplayName string '{opts.bundle_name}'", main_info_plist)

    with open("bundle_id.txt", "w") as f:
        if opts.force_original_id:
            f.write(old_main_bundle_id)
        else:
            f.write(main_bundle_id)

    component_exts = ["*.app", "*.appex", "*.framework", "*.dylib"]
    # make sure components are ordered depth-first, otherwise signing will overlap and become invalid
    components = [item for e in component_exts for item in main_app.glob("**/" + e)][::-1]
    components.append(main_app)

    mappings: Dict[str, str] = {}

    def sign_secondary(component: Path, workdir: Path):
        # entitlements of frameworks, etc. don't matter, so leave them (potentially) invalid
        print("Signing with original entitlements")
        return codesign_async(opts.common_name, component)

    def sign_primary(component: Path, workdir: Path):
        info_plist = component.joinpath("Info.plist")
        with tempfile.NamedTemporaryFile(dir=workdir, suffix=".plist", delete=False) as f:
            entitlements_plist = Path(f.name)
        embedded_prov = component.joinpath("embedded.mobileprovision")
        old_bundle_id = plist_buddy("Print :CFBundleIdentifier", info_plist)
        bundle_id = f"{main_bundle_id}{old_bundle_id[len(old_main_bundle_id):]}"
        component_bin = component.joinpath(component.stem)

        if opts.prov_file is not None:
            shutil.copy2(opts.prov_file, embedded_prov)
            # This may cause issues with wildcard entitlements, since they are valid in the provisioning
            # profile, but not when applied to a binary. For example:
            #   com.apple.developer.icloud-services = *
            # Ideally, all such cases should be manually replaced.
            dump_prov_entitlements_plist(embedded_prov, entitlements_plist)

            prov_app_id = plist_buddy("Print :application-identifier", entitlements_plist)
            component_app_id = f"{opts.team_id}.{bundle_id}"
            if prov_app_id == component_app_id or "*" in prov_app_id:
                plist_buddy(f"Set :application-identifier {component_app_id}", entitlements_plist)
            else:
                print(
                    f"WARNING: Provisioning profile's app id '{prov_app_id}' does not match component's app id '{component_app_id}'.",
                    "Using provisioning profile's app id - the component will run, but its entitlements will be broken!",
                    sep="\n",
                )
        else:
            with tempfile.TemporaryDirectory() as tmpdir_str:
                tmpdir = Path(tmpdir_str)
                simple_app_dir = tmpdir.joinpath("SimpleApp")
                shutil.copytree("SimpleApp", simple_app_dir)
                xcode_entitlements_plist = simple_app_dir.joinpath("SimpleApp/SimpleApp.entitlements")
                with open(xcode_entitlements_plist, "w") as f:
                    try:
                        s = codesign_dump_entitlements(component)
                    except:
                        print("Failed to dump entitlements, using empty")
                        s = plist_base
                    f.write(s)

                old_team_ids: Set[str] = set()
                try:
                    old_team_ids.add(
                        plist_buddy("Print :com.apple.developer.team-identifier", xcode_entitlements_plist)
                    )
                except:
                    print("Failed to read old team id from com.apple.developer.team-identifier")
                try:
                    old_team_ids.add(
                        plist_buddy("Print :application-identifier", xcode_entitlements_plist).split(".")[0]
                    )
                except:
                    print("Failed to read old team id from application-identifier")

                print("Original entitlements:", read_file(xcode_entitlements_plist), sep="\n")

                for item in [
                    # invalid Xcode entitlements
                    "application-identifier",
                    "com.apple.developer.team-identifier",
                    # the original value may be incompatible with the type of certificate, so let Xcode add the right one
                    "get-task-allow",
                    # inapplicable
                    "com.apple.developer.in-app-payments",
                    # special entitlements
                    # https://developer.apple.com/documentation/xcode/preparing-your-app-to-be-the-default-browser-or-email-client
                    "com.apple.developer.mail-client",
                    "com.apple.developer.web-browser",
                    # https://stackoverflow.com/questions/65330175/which-entitlements-are-special-entitlements-how-do-they-work
                    "com.apple.developer.networking.multicast",
                    "com.apple.developer.usernotifications.filtering",
                    "com.apple.developer.usernotifications.critical-alerts",
                    "com.apple.developer.networking.HotspotHelper",
                    "com.apple.managed.vpn.shared",
                    # only valid in app store distribution
                    # https://developer.apple.com/library/archive/qa/qa1830/_index.html
                    "beta-reports-active",
                    # https://developer.apple.com/documentation/carplay/requesting_the_carplay_entitlements
                    "com.apple.developer.carplay-messaging",
                    # https://stackoverflow.com/questions/62726152/provisioning-profile-doesnt-include-the-com-apple-developer-pushkit-unrestricte
                    "com.apple.developer.pushkit.unrestricted-voip",
                    # TODO: possible, but requires more complex parent-child app component relationship
                    # https://developer.apple.com/documentation/app_clips
                    "com.apple.developer.associated-appclip-app-identifiers",
                ]:
                    plist_buddy(
                        f"Delete :{item}",
                        xcode_entitlements_plist,
                        check=False,
                    )

                for entitlement, value in {
                    "com.apple.developer.icloud-container-environment": "Development",
                    "aps-environment": "development",
                }.items():
                    plist_buddy(
                        f"Set :{entitlement} {value}",
                        xcode_entitlements_plist,
                        check=False,
                    )

                patches: Dict[str, str] = {}

                for entitlement, prefix, parents in (
                    ("com.apple.security.application-groups", "group.", []),
                    (
                        "com.apple.developer.icloud-container-identifiers",
                        "iCloud.",
                        ["com.apple.developer.icloud-container-environment", "com.apple.developer.icloud-services"],
                    ),
                    ("com.apple.developer.ubiquity-container-identifiers", "iCloud.", []),
                ):
                    try:
                        remap_ids = plist_buddy(
                            "Print :" + entitlement,
                            xcode_entitlements_plist,
                        )
                    except:
                        remap_ids = ""

                    remap_ids = [remap_id.strip()[len(prefix) :] for remap_id in remap_ids.splitlines()[1:-1]]
                    if len(remap_ids) < 1:
                        # some features like iCloud only work with Xcode if they have identifiers defined
                        # make sure such cases are fixed if necessary
                        for parent in parents:
                            try:
                                # check if entitlement exists
                                plist_buddy(
                                    "Print :" + parent,
                                    xcode_entitlements_plist,
                                )
                                # add a default identifier
                                remap_ids.append(bundle_id)
                                break
                            except:
                                continue

                    if len(remap_ids) < 1:
                        continue

                    for remap_id in remap_ids:
                        if remap_id not in mappings:
                            if opts.encode_ids:
                                if opts.bundle_id:
                                    seed = opts.bundle_id
                                else:
                                    seed = remap_id + opts.team_id
                                mappings[remap_id] = gen_id(remap_id, seed)
                            else:
                                mappings[remap_id] = remap_id

                    plist_buddy(
                        "Delete :" + entitlement,
                        xcode_entitlements_plist,
                        check=False,
                    )
                    plist_buddy(
                        f"Add :{entitlement} array",
                        xcode_entitlements_plist,
                    )

                    for i, remap_id in enumerate(remap_ids):
                        plist_buddy(
                            f"Add :{entitlement}:{i} string '{prefix+mappings[remap_id]}'",
                            xcode_entitlements_plist,
                        )
                        patches[prefix + remap_id] = prefix + mappings[remap_id]

                for old_team_id in old_team_ids:
                    patches[old_team_id] = opts.team_id
                patches[old_bundle_id] = bundle_id
                patches[old_main_bundle_id] = main_bundle_id

                print("Applying patches...")
                targets = [xcode_entitlements_plist]
                if opts.patch_ids:
                    targets.append(component_bin)
                    targets.append(info_plist)
                else:
                    print("Skipping component binary")
                for target in targets:
                    for old, new in patches.items():
                        binary_replace(f"s/{re.escape(old)}/{re.escape(new)}/g", target)

                print("Patched entitlements:", read_file(xcode_entitlements_plist), sep="\n")

                simple_app_proj = simple_app_dir.joinpath("SimpleApp.xcodeproj")
                simple_app_pbxproj = simple_app_proj.joinpath("project.pbxproj")
                binary_replace(f"s/BUNDLE_ID_HERE_V9KP12/{bundle_id}/g", simple_app_pbxproj)
                binary_replace(f"s/DEV_TEAM_HERE_J8HK5C/{opts.team_id}/g", simple_app_pbxproj)

                for prov_profile in get_prov_profiles():
                    os.remove(prov_profile)

                print("Obtaining provisioning profile...")
                print("Archiving app...")
                archive = simple_app_dir.joinpath("archive.xcarchive")
                xcode_archive(simple_app_proj, "SimpleApp", archive)
                if is_distribution:
                    print("Exporting app...")
                    for prov_profile in get_prov_profiles():
                        os.remove(prov_profile)
                    xcode_export(simple_app_proj, archive, simple_app_dir)
                    exported_ipa = simple_app_dir.joinpath("SimpleApp.ipa")
                    extract_zip(exported_ipa, simple_app_dir)
                    output_bin = simple_app_dir.joinpath("Payload/SimpleApp.app")
                else:
                    output_bin = archive.joinpath("Products/Applications/SimpleApp.app")

                prov_profiles = list(get_prov_profiles())
                shutil.move(str(prov_profiles[0]), embedded_prov)
                for prov_profile in prov_profiles[1:]:
                    os.remove(prov_profile)
                with open(entitlements_plist, "w") as f:
                    f.write(codesign_dump_entitlements(output_bin))

        if opts.force_original_id:
            print("Keeping original CFBundleIdentifier")
            plist_buddy(f"Set :CFBundleIdentifier {old_bundle_id}", info_plist)
        else:
            print(f"Setting CFBundleIdentifier to {bundle_id}")
            plist_buddy(f"Set :CFBundleIdentifier {bundle_id}", info_plist)

        plist_buddy("Delete :get-task-allow", entitlements_plist, check=False)
        if opts.patch_debug:
            plist_buddy("Add :get-task-allow bool true", entitlements_plist)
            print("Enabled app debugging")
        else:
            print("Disabled app debugging")

        if opts.patch_all_devices:
            print("Force enabling support for all devices")
            plist_buddy("Delete :UISupportedDevices", info_plist, check=False)
            # https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/iPhoneOSKeys.html
            plist_buddy("Delete :UIDeviceFamily", info_plist, check=False)
            plist_buddy("Add :UIDeviceFamily array", info_plist)
            plist_buddy("Add :UIDeviceFamily:0 integer 1", info_plist)
            plist_buddy("Add :UIDeviceFamily:1 integer 2", info_plist)

        if opts.patch_file_sharing:
            print("Force enabling file sharing")
            plist_buddy("Delete :UIFileSharingEnabled", info_plist, check=False)
            plist_buddy("Add :UIFileSharingEnabled bool true", info_plist)
            plist_buddy("Delete :UISupportsDocumentBrowser", info_plist, check=False)
            plist_buddy("Add :UISupportsDocumentBrowser bool true", info_plist)

        print("Signing with entitlements:", read_file(entitlements_plist), sep="\n")
        return codesign_async(opts.common_name, component, entitlements_plist)

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        jobs: Dict[Path, subprocess.Popen[bytes]] = {}
        for component in components:
            print(f"Preparing component {component}")

            for path in list(jobs.keys()):
                pipe = jobs[path]
                try:
                    path.relative_to(component)
                except:
                    continue
                if pipe.poll() is None:
                    print("Waiting for sub-component to finish signing:", path)
                    pipe.wait()
                popen_check(pipe)
                jobs.pop(path)

            print("Processing")

            sc_info = component.joinpath("SC_Info")
            if sc_info.exists():
                print(f"Removing leftover AppStore data")
                shutil.rmtree(sc_info)

            if component.suffix in [".appex", ".app"]:
                jobs[component] = sign_primary(component, tmpdir)
            else:
                jobs[component] = sign_secondary(component, tmpdir)

        print("Waiting for any remaining components to finish signing")
        for pipe in jobs.values():
            pipe.wait()
            popen_check(pipe)
