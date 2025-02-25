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
    global metadata_cache, missing_packages, license_info
    
    # Use cached response if available
    if package in metadata_cache:
        if verbose:
            print(f"Using cached metadata for {package}", file=sys.stderr)
        
        # Extract license info if requested and not already cached
        if fetch_license and package not in license_info:
            license_info[package] = extract_license_info(metadata_cache[package])
            
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

def print_dependency_tree(tree, root_package, indent=0, visited=None, show_license=False):
    """Recursive printing to show the hierarchy in a 'pretty' format."""
    if visited is None:
        visited = set()

    clean_package = parse_requirement(root_package)
    prefix = "  " * indent
    
    if show_license and clean_package in license_info:
        license_text = license_info[clean_package]["license"]
        print(f"{prefix}- {root_package} [{license_text}]")
    else:
        print(f"{prefix}- {root_package}")
        
    visited.add(clean_package)

    for dep in tree.get(clean_package, []):
        clean_dep = parse_requirement(dep)
        if clean_dep not in visited:
            print_dependency_tree(tree, dep, indent + 1, visited, show_license)

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
    
    # Remove the root package
    if clean_root in all_packages:
        all_packages.remove(clean_root)
    
    # Count direct dependencies
    direct_deps = set(parse_requirement(dep) for dep in tree.get(clean_root, []))
    
    # Count dependencies by depth
    depth_counts = Counter(package_depths.values())
    
    # Print the report
    print(f"\nDEPENDENCY REPORT FOR {root_package}")
    print(f"================================{'=' * len(root_package)}")
    print(f"Total unique dependencies: {len(all_packages)}")
    print(f"Direct dependencies: {len(direct_deps)}")
    print(f"Max dependency depth: {max(package_depths.values()) if package_depths else 0}")
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
        
        # Print package with license info if requested
        if show_license and pkg in license_info:
            license_text = license_info[pkg]["license"]
            print(f"  {full_spec or pkg} [{license_text}]")
        else:
            print(f"  {full_spec or pkg}")
    
    # Print missing packages report if any
    print_missing_packages_report()
    
    # Print license report if requested
    if show_license:
        print_license_report()

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
            "missing_packages": len(missing)
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
    parser.add_argument("--json", "-j", action="store_true",
                       help="Output results in JSON format")
    parser.add_argument("--output", "-o", type=str, 
                       help="Write output to a file instead of stdout")
    args = parser.parse_args()
    
    # Use either max_depth or infinity
    max_depth = args.max_depth if args.max_depth is not None else float('inf')

    tree, package_depths = build_dependency_tree(
        args.package, 
        max_depth, 
        args.verbose,
        include_conditional=args.all_deps,
        include_dev=args.include_dev,
        fetch_license=args.license
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
        print_dependency_tree(tree, args.package, show_license=args.license)
        
        # Optionally print the report
        if args.report:
            print_dependency_report(tree, package_depths, args.package, show_license=args.license)
        elif args.missing:
            print_missing_packages_report()
        elif args.license and not args.report:
            # Print license report if --license is specified but not --report
            # (as --report already includes the license report)
            print_license_report()
            
        # Reset stdout if it was redirected
        if args.output and not args.json:
            sys.stdout.close()
            sys.stdout = original_stdout
            print(f"Output written to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()

