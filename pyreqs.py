#!/usr/bin/env python3
import argparse
import json
import requests
import re
import sys
import time
import threading
import os
from collections import defaultdict, deque, Counter

PYPI_URL = "https://pypi.org/pypi/{package}/json"

# Flag to control the spinner thread
spinner_active = False
processed_count = 0

# Cache for API responses to avoid redundant requests
metadata_cache = {}

# Track errors and missing packages
missing_packages = {}

# Store license information for packages
license_info = {}

# Store flags for packages that need further investigation
investigation_flags = {}

def needs_investigation(package_data):
    """Determine if a package needs further investigation for non-Python deps.
    Only flags packages that definitively require native dependencies in a standard CPython environment.
    """
    info = package_data.get("info", {})
    flags = []
    confidence = 0  # Tracking confidence level of our detection
    
    # Check for extension module indicators in keywords
    keywords = info.get("keywords", "")
    if isinstance(keywords, list):
        keywords = " ".join(keywords)
    elif keywords is None:
        keywords = ""
        
    keyword_indicators = ["c-extension", "rust", "cython", "ffi", "native"]
    if any(ext in keywords.lower() for ext in keyword_indicators):
        flags.append("Contains extension module keywords")
        confidence += 2
    
    # Check package classifiers - strong indicators
    classifiers = info.get("classifiers", []) or []
    
    # These classifiers definitively indicate native code
    strong_classifiers = [
        "Programming Language :: C",
        "Programming Language :: C++",
        "Programming Language :: Rust",
        "Programming Language :: Cython",
        "Topic :: Software Development :: Libraries :: Python Modules :: Foreign Function Interface"
    ]
    for classifier in classifiers:
        if any(term in classifier for term in strong_classifiers):
            flags.append(f"Uses native code: {classifier}")
            confidence += 3
    
    # Check for common FFI build dependencies that are DIRECT (not conditional)
    requires_dist = info.get("requires_dist", []) or []
    
    ffi_packages = ["cython", "cffi", "pybind11", "rust", "maturin", "setuptools-rust", "cmake"]
    critical_ffi = ["cffi", "rust", "cython", "pybind11"]
    
    for req in requires_dist:
        req_lower = req.lower()
        
        # Only count dependencies if they're DIRECT and unconditional
        if ";" not in req_lower and "extra ==" not in req_lower:
            for pkg in ffi_packages:
                if pkg in req_lower:
                    flags.append(f"Direct FFI dependency: {req}")
                    confidence += 3
                    # Critical FFIs get even more confidence
                    if any(crit in req_lower for crit in critical_ffi):
                        confidence += 1
    
    # Check if package has binaries/wheels with compiled code
    # Look at the available files for .so, .pyd, or non-pure Python wheels
    is_pure_python = True
    release_info = package_data.get("urls", []) or []
    wheel_types = set()
    has_platform_specific_wheels = False
    
    for release in release_info:
        filename = release.get("filename", "").lower()
        
        # Check for C extension module markers in filenames
        if any(ext in filename for ext in [".so", ".pyd", ".dll"]):
            is_pure_python = False
            flags.append("Contains compiled extension modules")
            confidence += 5
            break
            
        # Analyze wheel types in detail
        if filename.endswith(".whl"):
            # Parse wheel filename components
            try:
                # Remove .whl extension and split by '-'
                wheel_parts = filename[:-4].split('-')
                if len(wheel_parts) >= 3:
                    # Last three components are python tag, abi tag, platform tag
                    python_tag = wheel_parts[-3]
                    abi_tag = wheel_parts[-2]
                    platform_tag = wheel_parts[-1]
                    
                    # Pure Python wheels have 'none' abi and 'any' platform
                    if abi_tag == 'none' and platform_tag == 'any':
                        wheel_types.add("pure-python")
                    # ABI3 wheels are compatible with multiple Python versions
                    elif 'abi3' in abi_tag:
                        wheel_types.add("abi3")
                        has_platform_specific_wheels = True
                    # CPython specific ABI wheels
                    elif python_tag.startswith('cp') and abi_tag.startswith('cp'):
                        wheel_types.add("cpython-abi")
                        has_platform_specific_wheels = True
                    # Other platform-specific wheels
                    elif platform_tag != 'any':
                        wheel_types.add("platform-specific")
                        has_platform_specific_wheels = True
            except:
                # If parsing fails, fall back to simple check
                if "py3-none-any" not in filename:
                    has_platform_specific_wheels = True
    
    # Add appropriate flags based on wheel analysis
    if has_platform_specific_wheels:
        is_pure_python = False
        
        if "abi3" in wheel_types:
            flags.append("Contains ABI3 wheels (stabilized C-API, compiled code)")
            confidence += 4
        elif "cpython-abi" in wheel_types:
            flags.append("Contains CPython-specific ABI wheels (version-specific compiled code)")
            confidence += 4
        elif "platform-specific" in wheel_types:
            flags.append("Contains platform-specific wheels (likely compiled code)")
            confidence += 3
        else:
            flags.append("Non-pure Python wheel (likely contains compiled code)")
            confidence += 3
    
    # If pure Python is explicitly mentioned in classifiers, decrease confidence
    if any("Pure Python" in c for c in classifiers):
        confidence -= 3
        is_pure_python = True
    
    # Check description and summary but require strong evidence
    # Fix: Make sure description and summary are not None before using lower()
    description = info.get("description", "") or ""
    if not isinstance(description, str):
        description = ""
    else:
        description = description.lower()
        
    summary = info.get("summary", "") or ""
    if not isinstance(summary, str):
        summary = ""
    else:
        summary = summary.lower()
    
    # Strong indicators in text that definitely suggest native deps
    strong_indicators = [
        "c extension", "native extension", "rust extension", 
        "compiled extension", "wrapper around the c library", 
        "bindings for the c library", "rust implementation",
        "cython implementation", "binary module"
    ]
    
    # Check for the strongest indicators
    for indicator in strong_indicators:
        if indicator in description or indicator in summary:
            flags.append(f"Documentation explicitly mentions native code: '{indicator}'")
            confidence += 2
    
    # Only return flags if our confidence is high enough
    # This filters out packages that only have weak signals
    if confidence >= 3:
        return flags
    else:
        return []

def show_spinner():
    """Display a spinner with package count to indicate progress."""
    global spinner_active, processed_count
    spinner_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    i = 0
    
    while spinner_active:
        # Display spinner and package count, then return to start of line
        sys.stderr.write(f"\r{spinner_chars[i]} Processing dependencies... ({processed_count} packages)")
        sys.stderr.flush()
        time.sleep(0.1)
        i = (i + 1) % len(spinner_chars)
    
    # Clear the line when done
    sys.stderr.write("\r" + " " * 60 + "\r")
    sys.stderr.flush()

def extract_license_info(package_data):
    """Extract license information from PyPI metadata."""
    info = package_data.get("info", {})
    result = {
        "license": info.get("license") or "Unknown",
        "license_url": None,
        "project_url": info.get("project_url") or info.get("home_page"),
        "author": info.get("author"),
        "author_email": info.get("author_email"),
    }
    
    # Extract license URL from project URLs if available
    project_urls = info.get("project_urls", {})
    if project_urls:
        for key, url in project_urls.items():
            if "license" in key.lower():
                result["license_url"] = url
                break
    
    # Try to normalize common license names
    license_name = result["license"].lower()
    if "mit" in license_name:
        result["license"] = "MIT"
    elif "apache" in license_name:
        if "2.0" in license_name or "2" in license_name:
            result["license"] = "Apache-2.0"
        else:
            result["license"] = "Apache"
    elif "bsd" in license_name:
        if "3" in license_name:
            result["license"] = "BSD-3-Clause"
        elif "2" in license_name:
            result["license"] = "BSD-2-Clause"
        else:
            result["license"] = "BSD"
    elif "gpl" in license_name or "gnu general public" in license_name:
        if "v3" in license_name or "3" in license_name:
            result["license"] = "GPL-3.0"
        elif "v2" in license_name or "2" in license_name:
            result["license"] = "GPL-2.0"
        else:
            result["license"] = "GPL"
    elif "lgpl" in license_name:
        result["license"] = "LGPL"
    elif "mpl" in license_name or "mozilla" in license_name:
        result["license"] = "MPL"
    elif "public domain" in license_name:
        result["license"] = "Public Domain"
    elif "isc" in license_name:
        result["license"] = "ISC"
    
    return result

def get_pypi_metadata(package, verbose=False, parent=None, fetch_license=False):
    """Fetch metadata for a package from PyPI with caching."""
    global metadata_cache, missing_packages, license_info, investigation_flags
    
    # Use cached response if available
    if package in metadata_cache:
        if verbose:
            print(f"Using cached metadata for {package}", file=sys.stderr)
        
        # Extract license info if requested and not already cached
        if fetch_license and package not in license_info:
            license_info[package] = extract_license_info(metadata_cache[package])
            
        # Always check for investigation flags
        if package not in investigation_flags:
            flags = needs_investigation(metadata_cache[package])
            if flags:
                investigation_flags[package] = flags
                
        return metadata_cache[package]
    
    if verbose:
        print(f"Fetching metadata for {package}...", file=sys.stderr)
    
    url = PYPI_URL.format(package=package)
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        data = resp.json()
        
        # Cache the response
        metadata_cache[package] = data
        
        # Extract license information if requested
        if fetch_license:
            license_info[package] = extract_license_info(data)
            
        # Always check if package needs investigation
        flags = needs_investigation(data)
        if flags:
            investigation_flags[package] = flags
            
        return data
    except requests.HTTPError as e:
        error_msg = f"{e}"
        print(f"Warning: Could not get metadata for {package}: {error_msg}", file=sys.stderr)
        
        # Track the missing package and its parent
        if package not in missing_packages:
            missing_packages[package] = {
                'error': error_msg,
                'parents': set()
            }
        if parent:
            missing_packages[package]['parents'].add(parent)
            
        return None

def parse_requirement(req_string):
    """
    Parse a requirement string to extract just the package name.
    E.g. "requests (>=2.19.1)" -> "requests"
         "urllib3 [socks]" -> "urllib3"
         "charset-normalizer<4,>=2" -> "charset-normalizer"
    """
    # Remove extras (anything in square brackets)
    req_string = req_string.split('[', 1)[0].strip()
    
    # Remove version specifiers (anything in parentheses or after comparison operators)
    # Fix the deprecation warning by using named parameter
    req_string = re.split(r'[\s<>=!~;]', req_string, maxsplit=1)[0].strip()
    
    return req_string.lower()  # Normalize to lowercase for better comparison

def is_conditional_dependency(req_string):
    """
    Determine if a dependency is conditional or optional.
    Improved to catch more conditional/optional dependencies.
    """
    # Check for environment markers and extras
    if ';' in req_string:
        # Look for common optional dependency patterns
        lower_req = req_string.lower()
        optional_markers = [
            'extra ==', 'extra!=', 
            'platform_', 'sys_platform', 
            'implementation_name', 
            'python_version', 'python_full_version',
            'os_name', 'platform_machine'
        ]
        
        for marker in optional_markers:
            if marker in lower_req:
                return True
    
    # Check for explicit extras notation [extra]
    if '[' in req_string and ']' in req_string:
        return True
    
    return False

def is_dev_dependency(req_string):
    """Identify development, test, or doc dependencies"""
    lower_req = req_string.lower()
    
    # Common development-related package names
    dev_packages = [
        'pytest', 'nose', 'mock', 'coverage', 'flake8', 'pylint', 
        'sphinx', 'doc', 'test', 'dev', 'lint', 'check', 'tox', 'black',
        'isort', 'mypy', 'pep8', 'setuptools', 'wheel', 'build', 'twine',
        'typecheck', 'typing'
    ]
    
    # Check if dependency name is a common dev package
    pkg_name = parse_requirement(req_string)
    if any(dev in pkg_name for dev in dev_packages):
        return True
    
    # Check for dev-related extras or markers
    if any(marker in lower_req for marker in ['extra == "dev"', 'extra == "test"', 'extra == "docs"']):
        return True
    
    return False

def get_dependencies(package, version=None, verbose=False, include_conditional=False, include_dev=False, fetch_license=False):
    """
    Return a list of direct dependencies for the given package and version.
    We'll parse the 'requires_dist' field from PyPI JSON.
    """
    # Make sure to use just the package name for API call
    clean_package_name = parse_requirement(package)
    
    data = get_pypi_metadata(clean_package_name, verbose, parent=package, fetch_license=fetch_license)
    if data is None:
        return []
        
    if version is None:
        # Use the latest release version
        version = data["info"]["version"]
        if verbose:
            print(f"Using version {version} for {clean_package_name}", file=sys.stderr)

    # Acquire the 'requires_dist' for the chosen version
    release_info_list = data["releases"].get(version, [])
    if not release_info_list:
        requires_dist = data["info"].get("requires_dist", [])
    else:
        requires_dist = release_info_list[0].get("requires_dist", data["info"].get("requires_dist", []))

    deps = []
    filtered_conditional = 0
    filtered_dev = 0
    
    if requires_dist:
        for req in requires_dist:
            # Skip conditional dependencies unless explicitly requested
            if not include_conditional and is_conditional_dependency(req):
                filtered_conditional += 1
                continue
                
            # Skip dev dependencies unless explicitly requested
            if not include_dev and is_dev_dependency(req):
                filtered_dev += 1
                continue
            
            # Keep the original requirement text for display
            deps.append(req)
            
        if verbose:
            total_filtered = filtered_conditional + filtered_dev
            if total_filtered > 0:
                print(f"Found {len(deps)} core dependencies for {clean_package_name} (filtered {filtered_conditional} conditional and {filtered_dev} dev dependencies)", file=sys.stderr)
            else:
                print(f"Found {len(deps)} dependencies for {clean_package_name}", file=sys.stderr)
    else:
        if verbose:
            print(f"No dependencies found for {clean_package_name}", file=sys.stderr)
            
    return deps

def build_dependency_tree(root_package, max_depth=float('inf'), verbose=False, include_conditional=False, include_dev=False, fetch_license=False):
    """
    Build a dependency tree (dict) for the root_package up to max_depth.
    We'll do a BFS to store direct dependencies at each level.
    """
    global spinner_active, processed_count, missing_packages, license_info
    processed_count = 0
    missing_packages = {}  # Reset missing packages
    license_info = {}      # Reset license info
    
    if verbose:
        print(f"Building dependency tree for {root_package}...", file=sys.stderr)
        print(f"Options: {'include conditional deps' if include_conditional else 'core deps only'}, {'include dev deps' if include_dev else 'exclude dev deps'}, {'fetch license info' if fetch_license else 'no license info'}", file=sys.stderr)
    else:
        # Start spinner in a separate thread if not in verbose mode
        spinner_active = True
        spinner_thread = threading.Thread(target=show_spinner)
        spinner_thread.daemon = True
        spinner_thread.start()
    
    visited = set()
    tree = defaultdict(list)
    # Track the depth at which each package was first encountered
    package_depths = {}
    
    queue = deque([(root_package, 0)])
    
    try:
        while queue:
            package, depth = queue.popleft()
            clean_package = parse_requirement(package)
            
            if clean_package in visited or depth >= max_depth:
                continue
                
            # Store the first depth this package was encountered
            if clean_package not in package_depths:
                package_depths[clean_package] = depth
                
            if verbose:
                print(f"Processing {clean_package} (depth {depth})...", file=sys.stderr)
                
            visited.add(clean_package)
            processed_count += 1

            deps = get_dependencies(
                package, 
                verbose=verbose, 
                include_conditional=include_conditional,
                include_dev=include_dev,
                fetch_license=fetch_license
            )

            tree[clean_package] = deps
            for d in deps:
                clean_dep = parse_requirement(d)
                
                # Record parent relationship for missing packages
                if clean_dep in missing_packages and package not in missing_packages[clean_dep]['parents']:
                    missing_packages[clean_dep]['parents'].add(package)
                    
                if clean_dep not in visited:
                    queue.append((d, depth + 1))
    finally:
        # Stop the spinner thread
        if not verbose:
            spinner_active = False
            # Wait for the spinner to clean up
            time.sleep(0.2)
                    
    if verbose:
        print(f"Completed! Processed {processed_count} unique packages.", file=sys.stderr)
    else:
        print(f"Processed {processed_count} unique packages.", file=sys.stderr)
        
    return dict(tree), package_depths

def print_dependency_tree(tree, root_package, indent=0, visited=None, show_license=False, show_investigation=True):
    """Recursive printing to show the hierarchy in a 'pretty' format."""
    if visited is None:
        visited = set()

    clean_package = parse_requirement(root_package)
    prefix = "  " * indent
    
    # Build the output string with package name
    output = f"{prefix}- {root_package}"
    
    # Add license information if available
    if show_license and clean_package in license_info:
        license_text = license_info[clean_package]["license"]
        output += f" [{license_text}]"
    
    # Add investigation flags indicator if available
    if show_investigation and clean_package in investigation_flags:
        output += " (!)"  # Simple flag indicator
    
    print(output)
    
    # If package has investigation flags, print them at an increased indent
    if show_investigation and clean_package in investigation_flags:
        flag_prefix = "  " * (indent + 1)
        for flag in investigation_flags[clean_package]:
            print(f"{flag_prefix}! {flag}")
        
    visited.add(clean_package)

    for dep in tree.get(clean_package, []):
        clean_dep = parse_requirement(dep)
        if clean_dep not in visited:
            print_dependency_tree(tree, dep, indent + 1, visited, show_license, show_investigation)

def print_missing_packages_report():
    """Print a report of all missing packages."""
    global missing_packages
    
    if not missing_packages:
        return
        
    print("\nMISSING PACKAGES REPORT")
    print("======================")
    print(f"Total missing packages: {len(missing_packages)}")
    
    for pkg, info in sorted(missing_packages.items()):
        print(f"\n- {pkg}")
        print(f"  Error: {info['error']}")
        print(f"  Required by: {', '.join(sorted(info['parents'])) if info['parents'] else 'Unknown'}")
        
        # Try to give some advice about the package
        if "404" in info['error']:
            if "pypi.org" in info['error']:
                print("  Reason: This package is not available on PyPI. It might be:")
                print("          - A private/internal package")
                print("          - A GitHub repository directly referenced in requirements")
                print("          - A deprecated package that has been removed")
                print("          - A typo in the dependency specification")

def print_license_report():
    """Print a report of license information for all packages."""
    global license_info
    
    if not license_info:
        print("\nNo license information available. Run with --license to fetch license data.")
        return
    
    print("\nLICENSE REPORT")
    print("==============")
    print(f"Total packages with license info: {len(license_info)}")
    
    # Group packages by license type
    license_groups = defaultdict(list)
    for pkg_name, info in sorted(license_info.items()):
        license_type = info["license"]
        license_groups[license_type].append((pkg_name, info))
    
    # Print license groups
    print("\nLicense distribution:")
    for license_type, packages in sorted(license_groups.items()):
        print(f"  {license_type}: {len(packages)} packages")
    
    # Print detailed license information for each package
    print("\nDetailed license information:")
    for pkg_name, info in sorted(license_info.items()):
        print(f"\n- {pkg_name}")
        print(f"  License: {info['license']}")
        if info["license_url"]:
            print(f"  License URL: {info['license_url']}")
        if info["project_url"]:
            print(f"  Project URL: {info['project_url']}")
        if info["author"]:
            author_info = info["author"]
            if info["author_email"]:
                author_info += f" ({info['author_email']})"
            print(f"  Author: {author_info}")

def print_dependency_report(tree, package_depths, root_package, show_license=False):
    """Print a comprehensive report of all dependencies with their depths."""
    clean_root = parse_requirement(root_package)
    
    # Get all unique packages from the tree
    all_packages = set()
    for pkg, deps in tree.items():
        all_packages.add(pkg)
        for dep in deps:
            all_packages.add(parse_requirement(dep))
    
    # Make a copy of all packages including root for analysis
    all_packages_with_root = all_packages.copy()
    all_packages_with_root.add(clean_root)
    
    # Remove the root package from dependency count
    if clean_root in all_packages:
        all_packages.remove(clean_root)
    
    # Count direct dependencies
    direct_deps = set(parse_requirement(dep) for dep in tree.get(clean_root, []))
    
    # Count dependencies by depth
    depth_counts = Counter(package_depths.values())
    
    # Count packages requiring investigation - be sure to include root package if it needs investigation
    investigation_count = sum(1 for pkg in all_packages if pkg in investigation_flags)
    # Add root package to count if it needs investigation
    if clean_root in investigation_flags:
        investigation_count += 1
    
    # Helper function to get wheel type info for packages
    def get_wheel_info(package):
        data = metadata_cache.get(package)
        if not data:
            return None
            
        wheel_info = {
            "has_wheels": False,
            "wheel_types": [],
            "is_pure_python": True
        }
        
        release_info = data.get("urls", []) or []
        
        for release in release_info:
            filename = release.get("filename", "").lower()
            
            if filename.endswith(".whl"):
                wheel_info["has_wheels"] = True
                
                try:
                    # Parse wheel filename components
                    wheel_parts = filename[:-4].split('-')
                    if len(wheel_parts) >= 3:
                        # Last three components are python tag, abi tag, platform tag
                        python_tag = wheel_parts[-3]
                        abi_tag = wheel_parts[-2]
                        platform_tag = wheel_parts[-1]
                        
                        # Pure Python wheels
                        if abi_tag == 'none' and platform_tag == 'any':
                            if "pure-python" not in wheel_info["wheel_types"]:
                                wheel_info["wheel_types"].append("pure-python")
                        # ABI3 wheels
                        elif 'abi3' in abi_tag:
                            if "abi3" not in wheel_info["wheel_types"]:
                                wheel_info["wheel_types"].append("abi3")
                                wheel_info["is_pure_python"] = False
                        # CPython specific ABI wheels
                        elif python_tag.startswith('cp') and abi_tag.startswith('cp'):
                            if "cpython-abi" not in wheel_info["wheel_types"]:
                                wheel_info["wheel_types"].append("cpython-abi")
                                wheel_info["is_pure_python"] = False
                        # Other platform-specific wheels
                        elif platform_tag != 'any':
                            if "platform-specific" not in wheel_info["wheel_types"]:
                                wheel_info["wheel_types"].append("platform-specific")
                                wheel_info["is_pure_python"] = False
                except:
                    pass
                    
            # Check for C extension module markers in filenames
            if any(ext in filename for ext in [".so", ".pyd", ".dll"]):
                wheel_info["is_pure_python"] = False
                if "contains-extension-modules" not in wheel_info["wheel_types"]:
                    wheel_info["wheel_types"].append("contains-extension-modules")
        
        return wheel_info
    
    # Analyze wheel types
    wheel_type_counts = {
        "pure-python": 0,
        "abi3": 0,
        "cpython-abi": 0, 
        "platform-specific": 0,
        "contains-extension-modules": 0
    }
    
    # Count wheel types for all packages including root
    for pkg in all_packages_with_root:
        wheel_info = get_wheel_info(pkg)
        if wheel_info:
            for wheel_type in wheel_info["wheel_types"]:
                if wheel_type in wheel_type_counts:
                    wheel_type_counts[wheel_type] += 1
    
    # Print the report
    print(f"\nDEPENDENCY REPORT FOR {root_package}")
    print(f"================================{'=' * len(root_package)}")
    print(f"Total unique dependencies: {len(all_packages)}")
    print(f"Direct dependencies: {len(direct_deps)}")
    print(f"Max dependency depth: {max(package_depths.values()) if package_depths else 0}")
    print(f"Packages requiring investigation: {investigation_count}")
    
    # Print wheel type summary
    print("\nWheel type distribution:")
    for wheel_type, count in wheel_type_counts.items():
        if count > 0:
            print(f"  {wheel_type}: {count} packages")
    
    print("\nDependencies by depth:")
    for depth in sorted(depth_counts.keys()):
        if depth == 0:  # Skip root
            continue
        print(f"  Depth {depth}: {depth_counts[depth]} packages")
        
    # Print dependencies sorted by depth
    print("\nAll dependencies (sorted by depth):")
    sorted_deps = sorted(
        [(pkg, depth) for pkg, depth in package_depths.items() if pkg != clean_root],
        key=lambda x: (x[1], x[0])  # Sort by depth, then by name
    )
    
    current_depth = None
    for pkg, depth in sorted_deps:
        if current_depth != depth:
            current_depth = depth
            print(f"\n  --- Depth {depth} ---")
        
        # Find the full spec in the tree
        full_spec = None
        for parent, deps in tree.items():
            for dep in deps:
                if parse_requirement(dep) == pkg:
                    full_spec = dep
                    break
            if full_spec:
                break
        
        # Start with the package name and spec
        output = f"  {full_spec or pkg}"
        
        # Add license info if available
        if show_license and pkg in license_info:
            license_text = license_info[pkg]["license"]
            output += f" [{license_text}]"
            
        # Add wheel type if available
        wheel_info = get_wheel_info(pkg)
        if wheel_info and wheel_info["wheel_types"]:
            wheel_types_text = ", ".join(wheel_info["wheel_types"])
            output += f" (wheels: {wheel_types_text})"
            
        # Add investigation flag indicator if needed
        if pkg in investigation_flags:
            output += " (!)"
            
        print(output)
        
        # If package has investigation flags, print them indented
        if pkg in investigation_flags:
            for flag in investigation_flags[pkg]:
                print(f"    ! {flag}")
    
    # Print missing packages report if any
    print_missing_packages_report()
    
    # Print license report if requested
    if show_license:
        print_license_report()
        
    # Print investigation flags report if any
    if investigation_flags:
        print("\nPACKAGES REQUIRING FURTHER INVESTIGATION")
        print("=======================================")
        print(f"Total packages flagged: {investigation_count}")
        
        for pkg, flags in sorted(investigation_flags.items()):
            # Skip packages that aren't the root or in our dependency tree
            if pkg != clean_root and pkg not in all_packages:
                continue
                
            print(f"\n- {pkg}")
            for flag in flags:
                print(f"  • {flag}")
            print(f"  Recommendation: Verify system requirements and build environment")

def create_json_output(tree, package_depths, root_package):
    """
    Create a structured JSON representation of the dependency information.
    """
    clean_root = parse_requirement(root_package)
    
    # Get all packages from tree
    all_packages = set()
    for pkg, deps in tree.items():
        all_packages.add(pkg)
        for dep in deps:
            all_packages.add(parse_requirement(dep))
            
    # Helper function to get wheel information for a package
    def get_wheel_info(package):
        data = metadata_cache.get(package)
        if not data:
            return {}
            
        wheel_info = {
            "has_wheels": False,
            "wheel_types": [],
            "is_pure_python": True
        }
        
        release_info = data.get("urls", []) or []
        
        for release in release_info:
            filename = release.get("filename", "").lower()
            
            if filename.endswith(".whl"):
                wheel_info["has_wheels"] = True
                
                try:
                    # Parse wheel filename components
                    wheel_parts = filename[:-4].split('-')
                    if len(wheel_parts) >= 3:
                        # Last three components are python tag, abi tag, platform tag
                        python_tag = wheel_parts[-3]
                        abi_tag = wheel_parts[-2]
                        platform_tag = wheel_parts[-1]
                        
                        wheel_type = None
                        # Pure Python wheels
                        if abi_tag == 'none' and platform_tag == 'any':
                            wheel_type = "pure-python"
                        # ABI3 wheels
                        elif 'abi3' in abi_tag:
                            wheel_type = "abi3"
                            wheel_info["is_pure_python"] = False
                        # CPython specific ABI wheels
                        elif python_tag.startswith('cp') and abi_tag.startswith('cp'):
                            wheel_type = "cpython-abi"
                            wheel_info["is_pure_python"] = False
                        # Other platform-specific wheels
                        elif platform_tag != 'any':
                            wheel_type = "platform-specific"
                            wheel_info["is_pure_python"] = False
                            
                        if wheel_type and wheel_type not in wheel_info["wheel_types"]:
                            wheel_info["wheel_types"].append(wheel_type)
                except:
                    pass
                    
            # Check for C extension module markers in filenames
            if any(ext in filename for ext in [".so", ".pyd", ".dll"]):
                wheel_info["is_pure_python"] = False
                if "contains-extension-modules" not in wheel_info["wheel_types"]:
                    wheel_info["wheel_types"].append("contains-extension-modules")
        
        return wheel_info
    
    # Create a list of all dependencies with their details
    dependencies = []
    for pkg in sorted(all_packages):
        if pkg == clean_root:
            continue  # Skip the root package
            
        # Find the full spec in the tree
        full_spec = None
        direct_parents = []
        
        for parent, deps in tree.items():
            for dep in deps:
                dep_name = parse_requirement(dep)
                if dep_name == pkg:
                    if not full_spec:
                        full_spec = dep
                    direct_parents.append(parent)
        
        dep_info = {
            "name": pkg,
            "full_spec": full_spec or pkg,
            "depth": package_depths.get(pkg, -1),
            "direct_parents": direct_parents
        }
        
        # Get wheel information
        wheel_info = get_wheel_info(pkg)
        if wheel_info:
            dep_info.update(wheel_info)
        
        # Add license information if available
        if pkg in license_info:
            dep_info["license"] = license_info[pkg]["license"]
            if license_info[pkg]["license_url"]:
                dep_info["license_url"] = license_info[pkg]["license_url"]
            if license_info[pkg]["project_url"]:
                dep_info["project_url"] = license_info[pkg]["project_url"]
            if license_info[pkg]["author"]:
                dep_info["author"] = license_info[pkg]["author"]
                if license_info[pkg]["author_email"]:
                    dep_info["author_email"] = license_info[pkg]["author_email"]
        
        # Add investigation flags if available
        if pkg in investigation_flags:
            dep_info["investigation_required"] = True
            dep_info["investigation_flags"] = investigation_flags[pkg]
            dep_info["recommendation"] = "Verify system requirements and build environment"
        else:
            dep_info["investigation_required"] = False
        
        dependencies.append(dep_info)
    
    # Convert missing packages to a serializable format
    missing = []
    for pkg, info in missing_packages.items():
        missing.append({
            "name": pkg,
            "error": info["error"],
            "required_by": sorted(info["parents"])
        })
    
    # Create the final JSON structure
    json_data = {
        "root_package": root_package,
        "summary": {
            "total_dependencies": len(dependencies),
            "max_depth": max(package_depths.values()) if package_depths else 0,
            "missing_packages": len(missing),
            "packages_requiring_investigation": len(investigation_flags)
        },
        "dependencies": dependencies,
        "missing_packages": missing
    }
    
    # Add license summary if available
    if license_info:
        license_counts = {}
        for pkg, info in license_info.items():
            license_type = info["license"]
            license_counts[license_type] = license_counts.get(license_type, 0) + 1
        
        json_data["license_summary"] = {
            "packages_with_license_info": len(license_info),
            "license_distribution": license_counts
        }
    
    # Add wheel type summary
    wheel_type_counts = {
        "pure-python": 0,
        "abi3": 0,
        "cpython-abi": 0,
        "platform-specific": 0,
        "contains-extension-modules": 0
    }
    
    for dep in dependencies:
        for wheel_type in dep.get("wheel_types", []):
            if wheel_type in wheel_type_counts:
                wheel_type_counts[wheel_type] += 1
    
    # Only include non-zero counts
    json_data["wheel_summary"] = {k: v for k, v in wheel_type_counts.items() if v > 0}
    
    return json_data

def main():
    parser = argparse.ArgumentParser(description="Print dependency tree from PyPI.")
    parser.add_argument("package", help="Name of the root package.")
    parser.add_argument("--max-depth", type=int, default=None, 
                       help="Maximum depth of the dependency tree (default: unlimited)")
    parser.add_argument("--report", "-r", action="store_true",
                       help="Show a comprehensive dependency report")
    parser.add_argument("--verbose", "-v", action="store_true",
                       help="Enable verbose output")
    parser.add_argument("--all-deps", "-a", action="store_true",
                       help="Include conditional and optional dependencies (extras)")
    parser.add_argument("--include-dev", "-d", action="store_true",
                       help="Include development dependencies (test, docs, etc.)")
    parser.add_argument("--missing", "-m", action="store_true",
                       help="Show report of missing packages")
    parser.add_argument("--license", "-l", action="store_true",
                       help="Include license information for each package")
    parser.add_argument("--investigation", "-i", action="store_true",
                       help="Flag packages that may require further investigation (e.g., FFI, system dependencies)")
    parser.add_argument("--json", "-j", action="store_true",
                       help="Output results in JSON format")
    parser.add_argument("--output", "-o", type=str, 
                       help="Write output to a file instead of stdout")
    args = parser.parse_args()
    
    # Use either max_depth or infinity
    max_depth = args.max_depth if args.max_depth is not None else float('inf')
    
    # Always gather metadata fully for license and investigation
    fetch_metadata = True

    tree, package_depths = build_dependency_tree(
        args.package, 
        max_depth, 
        args.verbose,
        include_conditional=args.all_deps,
        include_dev=args.include_dev,
        fetch_license=fetch_metadata  # Always fetch full metadata to support all features
    )
    
    # Handle JSON output
    if args.json:
        json_data = create_json_output(tree, package_depths, args.package)
        json_output = json.dumps(json_data, indent=2)
        
        # Output to file if specified, otherwise to stdout
        if args.output:
            with open(args.output, 'w') as f:
                f.write(json_output)
            print(f"JSON output written to {args.output}", file=sys.stderr)
        else:
            print(json_output)
    else:
        # Standard text output
        if args.output:
            # Redirect stdout to file
            original_stdout = sys.stdout
            sys.stdout = open(args.output, 'w')
        
        # Always print the tree unless JSON was requested
        # Only show investigation details in tree if specifically requested
        print_dependency_tree(tree, args.package, show_license=args.license, show_investigation=args.investigation)
        
        # Optionally print the report
        if args.report:
            print_dependency_report(tree, package_depths, args.package, show_license=args.license)
        elif args.missing:
            print_missing_packages_report()
        elif args.license and not args.report:
            # Print license report if --license is specified but not --report
            # (as --report already includes the license report)
            print_license_report()
        elif args.investigation and not args.report:
            # Print investigation report if --investigation is specified but not --report
            # (as --report already includes the investigation report)
            if investigation_flags:
                print("\nPACKAGES REQUIRING FURTHER INVESTIGATION")
                print("=======================================")
                print(f"Total packages flagged: {len(investigation_flags)}")
                
                for pkg, flags in sorted(investigation_flags.items()):
                    print(f"\n- {pkg}")
                    for flag in flags:
                        print(f"  • {flag}")
                    print(f"  Recommendation: Verify system requirements and build environment")
            else:
                print("\nNo packages requiring further investigation were found.")
            
        # Reset stdout if it was redirected
        if args.output and not args.json:
            sys.stdout.close()
            sys.stdout = original_stdout
            print(f"Output written to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()

