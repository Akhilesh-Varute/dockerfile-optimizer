import os
import re
import time
import subprocess
from pathlib import Path
from dotenv import load_dotenv
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from typing import Tuple, Dict, Optional

# Load environment variables
load_dotenv()
console = Console()

# Check for available API keys
API_PROVIDERS = {
    "gemini": os.getenv("GEMINI_API_KEY"),
    "openai": os.getenv("OPENAI_API_KEY"),
    "claude": os.getenv("CLAUDE_API_KEY"),
    "perplexity": os.getenv("PERPLEXITY_API_KEY"),
}
selected_provider = None
selected_api_key = None
for provider, api_key in API_PROVIDERS.items():
    if api_key:
        selected_provider = provider
        selected_api_key = api_key
        break

if selected_provider == "gemini" and selected_api_key:
    genai.configure(api_key=selected_api_key)

# Constants
DOCKER_BEST_PRACTICES = [
    ("multi-stage", "Use multi-stage builds to reduce final image size"),
    ("non-root", "Create non-root user for security"),
    ("no-latest", "Avoid 'latest' tag for production images"),
    ("cache-layers", "Optimize layer caching order"),
    ("ignore-file", "Include .dockerignore file recommendations"),
    ("security-scan", "Suggest security scanning steps"),
    ("package-cleanup", "Clean package manager caches"),
    ("exposed-ports", "Properly manage exposed ports"),
    ("healthcheck", "Add healthcheck configuration"),
    ("labeling", "Include metadata labels"),
]


def validate_dockerfile(content: str) -> Tuple[bool, Dict[str, list]]:
    """Validate Dockerfile for critical security issues and anti-patterns."""
    issues = {"critical": [], "warnings": [], "recommendations": []}

    # Critical security checks
    if "curl | bash" in content.lower():
        issues["critical"].append("Insecure pipe installation detected")
    if "latest" in content.lower():
        issues["warnings"].append(
            "Using 'latest' tag is not recommended for production"
        )
    if "root" in content.lower() and "user " not in content.lower():
        issues["critical"].append("Running as root user detected")

    # Performance checks
    if "apt-get upgrade" in content.lower():
        issues["warnings"].append("Avoid 'apt-get upgrade' without pinning versions")

    # Check for missing --no-cache
    if "apt-get" in content.lower() and "--no-cache" not in content.lower():
        issues["warnings"].append("Missing --no-cache in apt-get commands")

    return len(issues["critical"]) == 0, issues


def enhanced_image_size_estimation(dockerfile_text: str) -> Tuple[float, float]:
    """More sophisticated estimation of Docker image sizes based on Dockerfile analysis.

    Returns:
        Tuple of (original_size_gb, optimized_size_gb)
    """
    # Initialize base size based on base image
    original_size = 1.0  # Default base size in GB

    # Extract base image with regex to get more precise info
    base_image_match = re.search(r"FROM\s+([^\s:]+):?([^\s]*)", dockerfile_text)
    base_image = ""
    base_tag = ""

    if base_image_match:
        base_image = base_image_match.group(1).lower()
        base_tag = (
            base_image_match.group(2).lower() if base_image_match.group(2) else ""
        )

        # More precise base image size estimation
        if "alpine" in base_image or "alpine" in base_tag:
            original_size = 0.4  # Alpine is smaller than previously estimated
        elif "scratch" in base_image:
            original_size = 0.2  # Scratch is minimal
        elif "slim" in base_tag:
            original_size = 0.7  # Slim variants are slightly smaller than estimated
        elif "ubuntu" in base_image:
            original_size = 1.3  # Ubuntu is slightly larger than estimated
        elif "debian" in base_image:
            original_size = 1.1  # Debian can vary
        elif "node" in base_image:
            if "alpine" in base_tag:
                original_size = 0.6  # Node alpine is larger than pure alpine
            else:
                original_size = 1.5  # Node is quite large
        elif "python" in base_image:
            if "alpine" in base_tag:
                original_size = 0.7  # Python alpine
            elif "slim" in base_tag:
                original_size = 0.9  # Python slim
            else:
                original_size = 1.3  # Full Python
        elif "golang" in base_image:
            if "alpine" in base_tag:
                original_size = 0.6
            else:
                original_size = 1.4
        elif "openjdk" in base_image or "java" in base_image:
            if "alpine" in base_tag:
                original_size = 1.0
            else:
                original_size = 1.6  # Java images are large

    # Count the number of layers to better estimate size
    run_layers = len(re.findall(r"\nRUN ", dockerfile_text))
    copy_layers = len(re.findall(r"\nCOPY ", dockerfile_text))
    add_layers = len(re.findall(r"\nADD ", dockerfile_text))

    # Estimate size of package installations (more granular)
    apt_get_installs = re.findall(
        r"apt-get\s+install\s+[^&|;]+", dockerfile_text.lower()
    )
    apt_package_count = 0
    for install_cmd in apt_get_installs:
        # Rough estimate of package count by counting words after 'install'
        words = install_cmd.split()
        if "install" in words:
            install_index = words.index("install")
            # Count words after 'install' that don't start with '-' (not options)
            apt_package_count += sum(
                1
                for w in words[install_index + 1 :]
                if not w.startswith("-") and w != "\\"
            )

    # Add for apt packages based on count
    original_size += apt_package_count * 0.05  # 50MB per package

    # NPM packages (check for package.json and node_modules)
    npm_installs = re.findall(r"npm\s+install", dockerfile_text.lower())
    yarn_installs = re.findall(r"yarn\s+install", dockerfile_text.lower())
    has_package_json = "package.json" in dockerfile_text

    npm_size = 0
    if has_package_json:
        if npm_installs or yarn_installs:
            # Production dependencies are usually smaller
            if (
                "--production" in dockerfile_text.lower()
                or "NODE_ENV=production" in dockerfile_text
            ):
                npm_size = 0.2
            else:
                npm_size = 0.4  # Dev dependencies included
    original_size += npm_size

    # Python packages
    pip_installs = re.findall(r"pip\s+install", dockerfile_text.lower())
    has_requirements = "requirements.txt" in dockerfile_text

    pip_size = 0
    if has_requirements or pip_installs:
        pip_size = 0.25
        # Check for heavy packages
        heavy_packages = [
            "tensorflow",
            "pytorch",
            "torch",
            "scipy",
            "pandas",
            "numpy",
            "scikit-learn",
        ]
        for pkg in heavy_packages:
            if pkg in dockerfile_text.lower():
                pip_size += 0.3  # Data science packages are large
    original_size += pip_size

    # Analyze COPY and ADD commands for large dataset transfers
    large_data_patterns = ["data", "dataset", "images", "models", "assets"]
    for pattern in large_data_patterns:
        if pattern in dockerfile_text.lower():
            original_size += 0.3  # Large data transfers impact size

    # Apply layer factor (imperfect layering adds overhead)
    layer_overhead = (run_layers + copy_layers + add_layers) * 0.02
    original_size += layer_overhead

    # Calculate optimized size based on more sophisticated rules
    optimized_size = original_size

    # Multi-stage builds provide significant optimization
    is_multi_stage = (
        "as builder" in dockerfile_text.lower() or "as build" in dockerfile_text.lower()
    )
    has_multiple_froms = len(re.findall(r"\nFROM ", dockerfile_text)) > 1

    if is_multi_stage or has_multiple_froms:
        # Multi-stage builds typically keep only what's necessary
        # Better estimation based on what's actually copied from builder
        copy_from_builders = len(re.findall(r"COPY\s+--from", dockerfile_text))
        if copy_from_builders > 0:
            # If only specific artifacts are copied, reduction is substantial
            optimized_size = original_size * 0.4  # 60% reduction
        else:
            # Less efficient multi-stage build
            optimized_size = original_size * 0.6  # 40% reduction
    else:
        # No multi-stage build, but there are other optimizations
        # Calculate potential reductions

        # Check for cleanup of package caches
        has_cache_cleanup = "rm -rf" in dockerfile_text and any(
            cache in dockerfile_text
            for cache in ["/var/cache", "apt-get clean", "npm cache", "pip cache"]
        )

        # Check for layer combination (using && between commands)
        has_combined_layers = "&&" in dockerfile_text

        reduction = 0.0
        if has_cache_cleanup:
            reduction += 0.1  # 10% reduction from cache cleanup
        if has_combined_layers:
            reduction += 0.1  # 10% reduction from layer optimization

        # Apply calculated reduction
        optimized_size = original_size * (1 - reduction)

        # Default minimum optimization (better practices in general)
        if optimized_size > original_size * 0.7:
            optimized_size = original_size * 0.7  # At least 30% reduction

    # Round to more realistic values
    original_size = round(original_size, 1)
    optimized_size = round(optimized_size, 1)

    return original_size, optimized_size


def enhanced_build_time_estimation(dockerfile_text: str) -> Tuple[int, int]:
    """More sophisticated estimation of Docker build times based on Dockerfile analysis.

    Returns:
        Tuple of (original_time_seconds, optimized_time_seconds)
    """
    # Base build time
    original_time = 30  # Baseline in seconds

    # Extract and analyze RUN commands for better timing estimates
    run_commands = re.findall(r"RUN\s+(.+?)(?:\n|$)", dockerfile_text)

    # Analyze each RUN command for time-consuming operations
    for cmd in run_commands:
        cmd_lower = cmd.lower()

        # Package management operations
        if "apt-get update" in cmd_lower:
            original_time += 15
        if "apt-get install" in cmd_lower:
            # Count packages being installed
            package_count = len(re.findall(r"[\w.-]+", cmd_lower))
            original_time += min(10 + package_count * 2, 60)  # Cap at 60 seconds

        # NPM operations
        if "npm install" in cmd_lower:
            if "--production" in cmd_lower:
                original_time += 40  # Smaller install
            else:
                original_time += 90  # Full dev dependencies

        # Yarn operations
        if "yarn install" in cmd_lower:
            if "--production" in cmd_lower or "--frozen-lockfile" in cmd_lower:
                original_time += 35
            else:
                original_time += 80

        # Python pip operations
        if "pip install" in cmd_lower:
            if "requirements.txt" in cmd_lower:
                original_time += 40
            else:
                # Count packages being installed
                package_count = len(re.findall(r"[\w.-]+", cmd_lower))
                original_time += min(15 + package_count * 3, 50)

        # Database operations
        if any(db in cmd_lower for db in ["mysql", "postgres", "mongodb"]):
            original_time += 20

        # Compilation and build processes
        if any(
            build in cmd_lower for build in ["make", "cmake", "gcc", "build", "compile"]
        ):
            original_time += 60

        # File operations
        if "wget" in cmd_lower or "curl" in cmd_lower:
            original_time += 15  # Download time
        if "tar" in cmd_lower or "unzip" in cmd_lower or "gunzip" in cmd_lower:
            original_time += 10  # Extraction time

        # Git operations
        if "git clone" in cmd_lower:
            original_time += 25  # Clone time
            if "depth=1" not in cmd_lower:
                original_time += 15  # Full history takes longer

    # COPY and ADD operations
    copy_commands = re.findall(r"COPY\s+(.+?)(?:\n|$)", dockerfile_text)
    add_commands = re.findall(r"ADD\s+(.+?)(?:\n|$)", dockerfile_text)

    # Estimate time for COPY operations
    for _ in copy_commands:
        original_time += 5  # Base file copy time

    # ADD operations might involve downloading or extracting
    for cmd in add_commands:
        if "http://" in cmd or "https://" in cmd:
            original_time += 15  # Remote file download
        elif ".tar" in cmd or ".gz" in cmd or ".zip" in cmd:
            original_time += 10  # Archive extraction
        else:
            original_time += 5  # Basic file copy

    # Analyze caching efficiency
    # Check for optimal ordering of commands (dependencies before code)
    has_dependency_first = False
    if dockerfile_text.lower().find("requirements.txt") < dockerfile_text.lower().find(
        "copy . ."
    ):
        has_dependency_first = True
    if dockerfile_text.lower().find("package.json") < dockerfile_text.lower().find(
        "copy . ."
    ):
        has_dependency_first = True

    # Analyze build context
    has_dockerignore = ".dockerignore" in dockerfile_text

    # Calculate optimized build time based on caching improvements
    optimized_time = original_time

    # Multi-stage builds
    is_multi_stage = (
        "as builder" in dockerfile_text.lower()
        or "as build" in dockerfile_text.lower()
        or len(re.findall(r"\nFROM ", dockerfile_text)) > 1
    )

    # Apply optimizations
    if is_multi_stage:
        # Multi-stage builds can parallelize and skip unnecessary steps
        optimized_time = int(original_time * 0.6)  # 40% reduction
    else:
        reduction = 0.0

        # Better caching with proper order
        if not has_dependency_first:
            # Potential improvement from reordering
            reduction += 0.15

        # Dockerfile ignores for faster context
        if not has_dockerignore:
            # Improvement from adding .dockerignore
            reduction += 0.1

        # Layer optimization (combining RUN commands)
        if len(run_commands) > 3 and "&&" not in dockerfile_text:
            # Potential for combining RUN commands
            reduction += 0.15

        # Apply calculated reduction
        optimized_time = int(original_time * (1 - reduction))

        # Default minimum optimization
        if optimized_time > int(original_time * 0.8):
            optimized_time = int(original_time * 0.8)  # At least 20% reduction

    return original_time, optimized_time


def generate_security_checklist(dockerfile_text: str) -> Dict[str, bool]:
    """Generate a security checklist based on Dockerfile content."""
    security_checks = {
        "Non-root user configured": "user " in dockerfile_text.lower(),
        "Specific version tags (no 'latest')": "latest" not in dockerfile_text.lower(),
        "Curl piped to shell": not (
            "curl" in dockerfile_text.lower() and " | " in dockerfile_text.lower()
        ),
        "Package cache cleanup": any(
            cache in dockerfile_text.lower()
            for cache in [
                "rm -rf /var/cache",
                "apt-get clean",
                "npm cache clean",
                "pip cache purge",
            ]
        ),
        "Exposed ports properly managed": "expose" in dockerfile_text.lower(),
        "Healthcheck configured": "healthcheck" in dockerfile_text.lower(),
        "Multi-stage build": any(
            pattern in dockerfile_text.lower()
            for pattern in ["as builder", "as build", "--from=", "multi-stage"]
        )
        or len(re.findall(r"\nFROM ", dockerfile_text)) > 1,
    }

    return security_checks


def analyze_environment_differences(dockerfile_text: str) -> Dict[str, Dict[str, str]]:
    """Analyze the differences between development and production environments in a Dockerfile.

    Returns:
        Dictionary with environment-specific behaviors and recommendations
    """
    env_analysis = {
        "development": {
            "size": "larger",
            "build_time": "longer",
            "layers": "more",
            "features": [],
            "recommendations": [],
        },
        "production": {
            "size": "optimized",
            "build_time": "faster",
            "layers": "fewer",
            "features": [],
            "recommendations": [],
        },
    }

    # Analyze dev vs prod patterns

    # Check for environment variables
    env_vars = re.findall(r"ENV\s+([A-Za-z0-9_]+)=([^\s]+)", dockerfile_text)
    node_env = next((v for k, v in env_vars if k == "NODE_ENV"), None)
    has_dev_mode = (
        "development" in dockerfile_text.lower() or "dev" in dockerfile_text.lower()
    )
    has_prod_mode = (
        "production" in dockerfile_text.lower() or "prod" in dockerfile_text.lower()
    )

    # Check for dev dependencies
    has_dev_deps = "devDependencies" in dockerfile_text or "--dev" in dockerfile_text

    # Check for debug tools
    debug_tools = [
        "vim",
        "nano",
        "curl",
        "wget",
        "telnet",
        "netcat",
        "nc",
        "strace",
        "gdb",
        "valgrind",
    ]
    has_debug_tools = any(tool in dockerfile_text.lower() for tool in debug_tools)

    # Check for multi-stage builds
    is_multi_stage = len(re.findall(r"\nFROM ", dockerfile_text)) > 1

    # Add feature detection
    if has_debug_tools:
        env_analysis["development"]["features"].append("Debug tools included")
        env_analysis["production"]["recommendations"].append(
            "Remove debugging tools in production"
        )

    if has_dev_deps:
        env_analysis["development"]["features"].append(
            "Development dependencies installed"
        )
        env_analysis["production"]["recommendations"].append(
            "Use --production flag for npm/yarn in production"
        )

    # Check for environment-specific instructions
    has_env_specific_instructions = (
        'if [ "$NODE_ENV" = "production" ]' in dockerfile_text
        or "ARG ENV" in dockerfile_text
    )

    if has_env_specific_instructions:
        env_analysis["development"]["features"].append(
            "Environment-specific conditional logic"
        )
        env_analysis["production"]["features"].append(
            "Environment-specific optimizations"
        )
    else:
        env_analysis["development"]["recommendations"].append(
            "Add environment-specific conditional logic (ARG ENV)"
        )
        env_analysis["production"]["recommendations"].append(
            "Use build arguments to create optimized production builds"
        )

    # Multi-stage recommendation
    if not is_multi_stage:
        env_analysis["production"]["recommendations"].append(
            "Implement multi-stage build for production"
        )
    else:
        env_analysis["production"]["features"].append(
            "Uses multi-stage build for minimal image size"
        )

    # Check if the same Dockerfile is used for both environments
    if has_dev_mode and has_prod_mode:
        env_analysis["development"]["features"].append(
            "Combined dev/prod Dockerfile with environment detection"
        )
        env_analysis["production"]["features"].append(
            "Combined dev/prod Dockerfile with environment detection"
        )
    else:
        if not has_env_specific_instructions:
            env_analysis["development"]["recommendations"].append(
                "Consider separate Dockerfiles (Dockerfile.dev and Dockerfile.prod)"
            )
            env_analysis["production"]["recommendations"].append(
                "Consider separate Dockerfiles (Dockerfile.dev and Dockerfile.prod)"
            )

    # Default recommendations if not enough features detected
    if len(env_analysis["development"]["features"]) < 2:
        env_analysis["development"]["features"].append("Standard build process")
        env_analysis["development"]["recommendations"].append(
            "Add dev-specific tools and dependencies"
        )

    if len(env_analysis["production"]["features"]) < 2:
        env_analysis["production"]["features"].append("Standard build process")
        env_analysis["production"]["recommendations"].append(
            "Optimize for size and security"
        )

    return env_analysis


def generate_env_optimized_dockerfile(
    dockerfile_text: str, preferred_base: str = "original"
) -> str:
    """Generate environment-optimized Dockerfile using ARG and multi-stage builds.

    This creates a template that uses build args to create either dev or prod builds.
    """
    # Analyze the current Dockerfile
    base_image_match = re.search(r"FROM\s+([^\s:]+):?([^\s]*)", dockerfile_text)
    base_image = base_image_match.group(1) if base_image_match else "alpine:3.16"
    base_tag = (
        base_image_match.group(2)
        if base_image_match and base_image_match.group(2)
        else ""
    )

    # Extract original image family (node, python, etc)
    image_family = ""
    for family in [
        "node",
        "python",
        "golang",
        "java",
        "openjdk",
        "ubuntu",
        "debian",
        "alpine",
    ]:
        if family in base_image.lower() or family in base_tag.lower():
            image_family = family
            break

    # Extract WORKDIR if present
    workdir_match = re.search(r"WORKDIR\s+([^\s]+)", dockerfile_text)
    workdir = workdir_match.group(1) if workdir_match else "/app"

    # Extract EXPOSE if present
    expose_match = re.search(r"EXPOSE\s+([0-9]+)", dockerfile_text)
    expose_port = expose_match.group(1) if expose_match else "8080"

    # Check if it's a Node.js application
    is_node = (
        "node" in dockerfile_text.lower()
        or "npm" in dockerfile_text.lower()
        or "yarn" in dockerfile_text.lower()
    )

    # Check if it's a Python application
    is_python = "python" in dockerfile_text.lower() or "pip" in dockerfile_text.lower()

    # Select appropriate base image based on preference
    node_base = ""
    python_base = ""

    if preferred_base == "alpine":
        node_base = "node:16-alpine"
        python_base = "python:3.9-alpine"
    elif preferred_base == "slim":
        node_base = "node:16-slim"
        python_base = "python:3.9-slim"
    elif preferred_base == "full":
        node_base = "node:16"
        python_base = "python:3.9"
    else:  # original
        # Try to preserve original image with version
        if base_image_match:
            original_full = f"{base_image}:{base_tag}" if base_tag else base_image
            node_base = original_full if image_family == "node" else "node:16"
            python_base = original_full if image_family == "python" else "python:3.9"
        else:
            node_base = "node:16"
            python_base = "python:3.9"

    # Create optimized template based on application type
    if is_node:
        return f"""# Optimized Dockerfile with dev/prod environments
# Usage:
# Development: docker build --build-arg ENV=development -t myapp:dev .
# Production: docker build --build-arg ENV=production -t myapp:prod .

# Build stage
FROM {node_base} AS builder
ARG ENV=production
WORKDIR {workdir}

# Copy package files first for better caching
COPY package*.json ./
RUN if [ "$ENV" = "development" ]; then \\
      npm install; \\
    else \\
      npm ci --only=production; \\
    fi

# Copy application code
COPY . .

# Build if needed (e.g., for TypeScript, Next.js, etc.)
RUN if [ -f "tsconfig.json" ]; then \\
      npm run build; \\
    fi

# Production stage (smaller image)
FROM {node_base} AS production
ARG ENV=production
WORKDIR {workdir}
ENV NODE_ENV=$ENV

# Copy only necessary files from builder
COPY --from=builder {workdir}/package*.json ./
COPY --from=builder {workdir}/node_modules ./node_modules

# For builds like Next.js, React, etc.
COPY --from=builder {workdir}/.next ./.next 2>/dev/null || true
COPY --from=builder {workdir}/build ./build 2>/dev/null || true
COPY --from=builder {workdir}/dist ./dist 2>/dev/null || true

# Add development tools if in dev environment
RUN if [ "$ENV" = "development" ]; then \\
      {get_install_command(node_base)} vim curl; \\
    fi

# Create non-root user for security
{get_user_creation_command(node_base)}
USER appuser

# Expose port
EXPOSE {expose_port}

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \\
  CMD wget --no-verbose --tries=1 --spider http://localhost:{expose_port}/health || exit 1

# Start the application
CMD ["npm", "start"]
"""
    elif is_python:
        return f"""# Optimized Dockerfile with dev/prod environments
# Usage:
# Development: docker build --build-arg ENV=development -t myapp:dev .
# Production: docker build --build-arg ENV=production -t myapp:prod .

# Build stage
FROM {python_base} AS builder
ARG ENV=production
WORKDIR {workdir}

# Install build dependencies
RUN {get_install_command(python_base)} gcc \\
    && {get_cleanup_command(python_base)}

# Copy requirements first for better caching
COPY requirements*.txt ./
RUN if [ "$ENV" = "development" ] && [ -f "requirements-dev.txt" ]; then \\
      pip install --no-cache-dir -r requirements-dev.txt; \\
    else \\
      pip install --no-cache-dir -r requirements.txt; \\
    fi

# Copy application code
COPY . .

# Production stage (smaller image)
FROM {python_base} AS production
ARG ENV=production
WORKDIR {workdir}
ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    ENVIRONMENT=$ENV

# Copy only necessary files from builder
COPY --from=builder {workdir}/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY --from=builder {workdir} ./

# Add development tools if in dev environment
RUN if [ "$ENV" = "development" ]; then \\
      {get_install_command(python_base)} vim curl \\
      && {get_cleanup_command(python_base)}; \\
    fi

# Create non-root user for security
{get_user_creation_command(python_base)}
USER appuser

# Expose port
EXPOSE {expose_port}

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \\
  CMD curl --fail http://localhost:{expose_port}/health || exit 1

# Start the application
CMD ["python", "app.py"]
"""
    else:
        # Generic template
        return f"""# Optimized Dockerfile with dev/prod environments
# Usage:
# Development: docker build --build-arg ENV=development -t myapp:dev .
# Production: docker build --build-arg ENV=production -t myapp:prod .

# Build stage
FROM {base_image}{':'+base_tag if base_tag else ''} AS builder
ARG ENV=production
WORKDIR {workdir}

# Copy application code
COPY . .

# Install dependencies based on environment
RUN if [ "$ENV" = "development" ]; then \\
      echo "Installing development dependencies"; \\
    else \\
      echo "Installing production dependencies"; \\
    fi

# Production stage
FROM {base_image}{':'+base_tag if base_tag else ''} AS production
ARG ENV=production
WORKDIR {workdir}

# Copy from builder stage
COPY --from=builder {workdir} ./

# Add development tools if in dev environment
RUN if [ "$ENV" = "development" ]; then \\
      echo "Installing development tools"; \\
    fi

# Create non-root user for security
{get_user_creation_command(base_image)}
USER appuser

# Expose port
EXPOSE {expose_port}

# Start the application
CMD ["echo", "Application started"]
"""


def get_install_command(image_name: str) -> str:
    """Get the appropriate package installation command based on the image."""
    if "alpine" in image_name.lower():
        return "apk add --no-cache"
    elif (
        any(x in image_name.lower() for x in ["debian", "ubuntu"])
        or "slim" in image_name.lower()
    ):
        return "apt-get update && apt-get install -y --no-install-recommends"
    else:
        # Default to apt-get for unknown images
        return "apt-get update && apt-get install -y --no-install-recommends"


def get_cleanup_command(image_name: str) -> str:
    """Get the appropriate cleanup command based on the image."""
    if "alpine" in image_name.lower():
        return "rm -rf /var/cache/apk/*"
    elif (
        any(x in image_name.lower() for x in ["debian", "ubuntu"])
        or "slim" in image_name.lower()
    ):
        return "rm -rf /var/lib/apt/lists/*"
    else:
        # Default to apt cleanup for unknown images
        return "rm -rf /var/lib/apt/lists/*"


def get_user_creation_command(image_name: str) -> str:
    """Get the appropriate user creation command based on the image."""
    if "alpine" in image_name.lower():
        return "RUN addgroup -S appgroup && adduser -S appuser -G appgroup"
    elif (
        any(x in image_name.lower() for x in ["debian", "ubuntu"])
        or "slim" in image_name.lower()
    ):
        return "RUN groupadd -r appgroup && useradd -r -g appgroup appuser"
    else:
        # Default to debian-style for unknown images
        return "RUN groupadd -r appgroup && useradd -r -g appgroup appuser"


def display_summary_table(
    dockerfile_path: str,
    original_size: float,
    optimized_size: float,
    original_time: int,
    optimized_time: int,
    security_checks: Dict[str, bool],
    env_analysis: Dict[str, Dict[str, str]] = None,
) -> None:
    """Display a summary table of Dockerfile metrics and security checks."""
    # Create a rich table for displaying metrics
    table = Table(title="Dockerfile Optimization Summary", show_header=True)

    # Add columns to the table
    table.add_column("Metric", style="cyan")
    table.add_column("Original", style="yellow")
    table.add_column("Optimized", style="green")
    table.add_column("Change", style="magenta")

    # Add rows for size and build time metrics
    size_reduction = int((1 - optimized_size / original_size) * 100)
    time_reduction = int((1 - optimized_time / original_time) * 100)

    # Format the optimized size in MB if it's below 1GB for better readability
    original_size_str = f"{original_size:.1f}GB"
    if optimized_size < 1.0:
        optimized_size_str = f"{int(optimized_size*1000)}MB"
    else:
        optimized_size_str = f"{optimized_size:.1f}GB"

    table.add_row(
        "Image Size",
        original_size_str,
        optimized_size_str,
        f"{size_reduction}% smaller",
    )

    table.add_row(
        "Build Time",
        f"{original_time}s",
        f"{optimized_time}s",
        f"{time_reduction}% faster",
    )

    # Print the metrics table
    console.print("\n📊 Optimization Metrics:", style="bold blue")
    console.print(table)

    # Create a security table
    security_table = Table(title="Security Analysis", show_header=True)
    security_table.add_column("Check", style="cyan")
    security_table.add_column("Status", style="yellow")

    # Add security check rows
    for check, passed in security_checks.items():
        status = "✅ Pass" if passed else "❌ Fail"
        status_style = "green" if passed else "red"
        security_table.add_row(check, f"[{status_style}]{status}[/{status_style}]")

    # Print the security table
    console.print("\n🔒 Security Checks:", style="bold blue")
    console.print(security_table)

    # Display environment differences if available
    if env_analysis:
        env_table = Table(title="Environment Differences", show_header=True)
        env_table.add_column("Environment", style="cyan")
        env_table.add_column("Features", style="green")
        env_table.add_column("Recommendations", style="yellow")

        # Development environment row
        dev_features = "\n".join(
            [f"• {f}" for f in env_analysis["development"]["features"]]
        )
        dev_recommendations = "\n".join(
            [f"• {r}" for r in env_analysis["development"]["recommendations"]]
        )
        env_table.add_row("Development", dev_features, dev_recommendations)

        # Production environment row
        prod_features = "\n".join(
            [f"• {f}" for f in env_analysis["production"]["features"]]
        )
        prod_recommendations = "\n".join(
            [f"• {r}" for r in env_analysis["production"]["recommendations"]]
        )
        env_table.add_row("Production", prod_features, prod_recommendations)

        console.print("\n🌐 Environment Analysis:", style="bold blue")
        console.print(env_table)

    # Print file path information
    console.print(f"\nDockerfile path: {dockerfile_path}", style="dim")


def generate_optimization_prompt(dockerfile_text: str) -> str:
    """Generate structured prompt for Gemini AI with best practice enforcement and enhanced metrics."""

    original_size, optimized_size = enhanced_image_size_estimation(dockerfile_text)
    original_time, optimized_time = enhanced_build_time_estimation(dockerfile_text)
    security_checks = generate_security_checklist(dockerfile_text)
    env_analysis = analyze_environment_differences(dockerfile_text)

    # Format estimated metrics for the prompt
    size_reduction = int((1 - optimized_size / original_size) * 100)
    time_reduction = int((1 - optimized_time / original_time) * 100)

    # Format the optimized size in MB if it's below 1GB for better readability
    original_size_str = f"{original_size:.1f}GB"
    if optimized_size < 1.0:
        optimized_size_str = f"{int(optimized_size*1000)}MB"
    else:
        optimized_size_str = f"{optimized_size:.1f}GB"

    # Format environment differences
    dev_features = "\n".join(
        [f"- {f}" for f in env_analysis["development"]["features"]]
    )
    dev_recommendations = "\n".join(
        [f"- {r}" for r in env_analysis["development"]["recommendations"]]
    )
    prod_features = "\n".join(
        [f"- {f}" for f in env_analysis["production"]["features"]]
    )
    prod_recommendations = "\n".join(
        [f"- {r}" for r in env_analysis["production"]["recommendations"]]
    )

    env_section = f"""
## 🔀 Environment-Specific Differences

### Development Environment
Features:
{dev_features}

Recommendations:
{dev_recommendations}

### Production Environment
Features:
{prod_features}

Recommendations:
{prod_recommendations}
"""

    metrics_section = f"""
## 📊 Metrics
- 🔄 Build Time Estimate:
  Before: {original_time}s | After: {optimized_time}s ({time_reduction}% reduction)
- 📦 Image Size Comparison: 
  Original: {original_size_str} → Optimized: {optimized_size_str} ({size_reduction}% smaller)
- 🔒 Security Checklist:
  {''.join([f"✅ {check}\\n" if passed else f"❌ {check}\\n" for check, passed in security_checks.items()])}
"""

    return f"""
You are a Docker expert. Analyze this Dockerfile following strict best practices:

**Mandatory Requirements:**
1. Enforce multi-stage builds when possible[1][7]
2. Require non-root user configuration[4][7]
3. Specify exact base image versions (no 'latest')[1][5]
4. Optimize layer caching order[1][3]
5. Include security scanning recommendations[6][8]
6. Support both development and production environments with conditional statements [9]

**Analysis Template:**
---
## 🔍 Comprehensive Analysis

### Security Issues
<list vulnerabilities with CVE references if possible>

### Performance Optimization
<layer-by-layer optimization opportunities>

### Best Practice Violations
<list violations with Docker documentation references>

### Environment Configuration
<identify environment-specific configurations and their impact>

---

## 🛠️ Optimization Plan

### Required Fixes
<critical security fixes>

### Recommended Improvements
<performance/best practice enhancements>

### Environment-Specific Optimizations
<development vs production specific optimizations>

---

## ✅ Optimized Dockerfile
<full Dockerfile code with comments supporting both dev and prod environments using ARG ENV>

---
{metrics_section}
---
{env_section}
---

## 🔒 Security Checklist
{'\n'.join([f"- [{'x' if i < 3 else ' '}] {desc}" for i, (_, desc) in enumerate(DOCKER_BEST_PRACTICES)])}

---

## 🚀 Validation Commands
# Development build
docker build --build-arg ENV=development -t myapp:dev .

# Production build
docker build --build-arg ENV=production -t myapp:prod .

# Scan production image
docker scan myapp:prod

# Compare image sizes
docker images myapp:dev myapp:prod

Dockerfile to analyze:
{dockerfile_text}
"""


def optimize_dockerfile(dockerfile_text: str, prompt: str = None) -> str:
    """Optimize Dockerfile using an AI provider (Gemini, OpenAI, Claude, or Perplexity)."""
    # Initial validation
    is_valid, issues = validate_dockerfile(dockerfile_text)
    if not is_valid:
        console.print("\n❌ Critical issues found:", style="bold red")
        for issue in issues["critical"]:
            console.print(f"  - {issue}", style="red")
        raise ValueError("Dockerfile validation failed")

    # Generate optimized prompt if not provided
    if prompt is None:
        prompt = generate_optimization_prompt(dockerfile_text)

    # Check for selected provider
    if not selected_provider or not selected_api_key:
        raise ValueError(
            "No valid API key found. Set GEMINI_API_KEY, OPENAI_API_KEY, CLAUDE_API_KEY, or PERPLEXITY_API_KEY in .env"
        )

    # Handle different AI providers
    response_text = ""
    if selected_provider == "gemini":
        # Configure model with safety settings
        generation_config = genai.types.GenerationConfig(
            temperature=0.3, top_p=0.95, max_output_tokens=4096
        )
        model = genai.GenerativeModel("gemini-1.5-flash")  # Updated to a valid model
        response = model.generate_content(
            prompt,
            generation_config=generation_config,
            safety_settings={
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
            },
        )
        response_text = response.text
    else:
        # Placeholder for other providers (OpenAI, Claude, Perplexity)
        console.print(
            f"\n⚠️ {selected_provider.capitalize()} API not yet implemented. Using prompt as fallback.",
            style="yellow",
        )
        response_text = (
            f"Optimization prompt generated for {selected_provider}:\n{prompt}"
        )

    # Debug: Log response details
    console.print(
        f"[debug] {selected_provider.capitalize()} response length: {len(response_text)} chars",
        style="yellow",
    )
    console.print(
        f"[debug] {selected_provider.capitalize()} response snippet: {response_text[:200]}...",
        style="yellow",
    )

    return response_text


def generate_dockerignore(repo_path: str, prompt_user: bool = True) -> None:
    """Generate .dockerignore file based on project contents if user confirms."""
    dockerignore_path = Path(repo_path) / ".dockerignore"
    if not dockerignore_path.exists():
        if prompt_user:
            console.print("\n💡 No .dockerignore file found.", style="yellow")
            response = (
                input("Would you like to create a .dockerignore file? (y/n): ")
                .strip()
                .lower()
            )
            if response != "y":
                console.print("⏭️ Skipping .dockerignore file creation.", style="yellow")
                return

        with open(dockerignore_path, "w") as f:
            f.write(
                "\n".join(
                    [
                        "# Auto-generated Dockerignore",
                        "**/node_modules",
                        "**/__pycache__",
                        "*.log",
                        ".git",
                        ".env",
                        "Dockerfile.dev",
                    ]
                )
            )
        console.print(
            f"✅ Generated .dockerignore at {dockerignore_path}", style="green"
        )


def apply_optimized_dockerfile(dockerfile_path: str, optimized_content: str) -> None:
    """Apply the optimized Dockerfile if user confirms."""
    console.print(
        "\n🔄 Ready to update Dockerfile with optimized version", style="yellow"
    )
    response = (
        input("Would you like to apply these optimizations to your Dockerfile? (y/n): ")
        .strip()
        .lower()
    )

    if response == "y":
        # Create backup of original Dockerfile
        backup_path = dockerfile_path + ".backup"
        try:
            with open(dockerfile_path, "r") as src, open(backup_path, "w") as dst:
                dst.write(src.read())
            console.print(
                f"✅ Original Dockerfile backed up to {backup_path}", style="green"
            )

            # Write optimized Dockerfile
            with open(dockerfile_path, "w") as f:
                f.write(optimized_content)
            console.print(
                f"✅ Dockerfile updated with optimizations at {dockerfile_path}",
                style="green",
            )
        except Exception as e:
            console.print(
                f"❌ Error applying optimizations: {str(e)}", style="bold red"
            )
    else:
        console.print(
            "⏭️ Skipping Dockerfile optimization. You can manually apply the changes.",
            style="yellow",
        )


def extract_optimized_dockerfile(ai_result: str) -> Optional[str]:
    """Extract the optimized Dockerfile content from the AI result."""
    # Try to find the Dockerfile section in the AI response
    dockerfile_match = re.search(
        r"## ✅ Optimized Dockerfile\s+(.*?)(?:\n[-─]{3,}|\n##|\Z)",
        ai_result,
        re.DOTALL,
    )
    if dockerfile_match:
        content = dockerfile_match.group(1).strip()
        # Remove any markdown code block markers
        content = re.sub(r"```dockerfile|```|`", "", content)
        return content
    return None


def suggest_distroless_alternative(
    base_image: str, preferred_base: str = "original"
) -> str:
    """Suggest appropriate distroless image based on the current base image and user preference."""
    distroless_map = {
        "python": "gcr.io/distroless/python3",
        "node": "gcr.io/distroless/nodejs",
        "java": "gcr.io/distroless/java",
        "go": "gcr.io/distroless/static",
        "debian": "gcr.io/distroless/base",
        "ubuntu": "gcr.io/distroless/base",
    }

    # If user wants to keep original type, suggest appropriate distroless
    for key, value in distroless_map.items():
        if key in base_image.lower():
            return value

    # If no specific match and not preserving original, use default
    return "gcr.io/distroless/base"


def add_vulnerability_scanning_section() -> str:
    """Generate vulnerability scanning recommendations for documentation."""
    return """
## 🔒 Vulnerability Scanning Integration

Add these commands to your CI/CD pipeline to ensure security:

```bash
# Scan with Trivy (Recommended)
docker build -t myapp:prod .
trivy image --severity HIGH,CRITICAL myapp:prod

# Or use Docker Scout
docker scout cves myapp:prod

# Regular scanning with cron job
echo "0 2 * * * docker run -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy image --severity HIGH,CRITICAL myapp:prod" | sudo tee -a /etc/crontab
```

Ensure your CI pipeline fails when vulnerabilities above a certain threshold are found:

```yaml
# Example GitHub Actions step
- name: Scan for vulnerabilities
  run: |
    trivy image --exit-code 1 --severity CRITICAL myapp:prod
```
"""


def integrate_vulnerability_scanning(dockerfile_text: str) -> str:
    """Modify the Dockerfile to include vulnerability scanning comments."""
    scanning_comment = """
# Security Scanning:
# After building this image, scan it for vulnerabilities with:
# trivy image --severity HIGH,CRITICAL $(docker images -q | head -n 1)
# or
# docker scout cves $(docker images -q | head -n 1)
"""

    # Add the comment near the top of the Dockerfile after any FROM statements
    import re

    # Find the last FROM statement
    last_from_index = dockerfile_text.rfind("FROM ")
    if last_from_index == -1:
        # No FROM statement found, add comment at the top
        return scanning_comment + dockerfile_text

    # Find the end of the line containing the last FROM statement
    end_of_line = dockerfile_text.find("\n", last_from_index)
    if end_of_line == -1:
        end_of_line = len(dockerfile_text)

    # Insert the scanning comment after this line
    return (
        dockerfile_text[: end_of_line + 1]
        + scanning_comment
        + dockerfile_text[end_of_line + 1 :]
    )


def recommend_sbom_generation() -> str:
    """Generate recommendations for SBOM creation and management."""
    return """
## 📦 Software Bill of Materials (SBOM)

Adding SBOM generation to your pipeline provides transparency and improves security:

```bash
# Generate SBOM with Syft
syft myapp:prod -o json > sbom.json

# Or use Docker Buildx
docker buildx build --sbom=true -t myapp:prod .

# Verify SBOM contents
grype sbom:./sbom.json

# Store SBOMs in artifact repository for compliance
curl -X PUT -H "Content-Type: application/json" -d @sbom.json https://your-artifact-repo/sboms/myapp-$(date +%Y%m%d).json
```

Include SBOM verification in your CI/CD pipeline to detect vulnerabilities early:

```yaml
# Example GitHub Actions step
- name: Generate SBOM
  run: syft myapp:prod -o json > sbom.json
  
- name: Scan SBOM for vulnerabilities
  run: grype sbom:./sbom.json --fail-on high
```
"""


def detect_hardcoded_secrets(dockerfile_text: str) -> list:
    """Detect potential hardcoded secrets in Dockerfile."""
    import re

    # Patterns that might indicate secrets
    secret_patterns = [
        (r'(?i)password\s*=\s*[\'\"][^\'"]+[\'\"]', "Password"),
        (r'(?i)passwd\s*=\s*[\'\"][^\'"]+[\'\"]', "Password"),
        (r'(?i)pwd\s*=\s*[\'\"][^\'"]+[\'\"]', "Password"),
        (r'(?i)secret\s*=\s*[\'\"][^\'"]+[\'\"]', "Secret"),
        (r'(?i)token\s*=\s*[\'\"][^\'"]+[\'\"]', "Token"),
        (r'(?i)api[-_]?key\s*=\s*[\'\"][^\'"]+[\'\"]', "API Key"),
        (r'(?i)auth[-_]?token\s*=\s*[\'\"][^\'"]+[\'\"]', "Auth Token"),
        (r'(?i)credentials\s*=\s*[\'\"][^\'"]+[\'\"]', "Credentials"),
        # AWS specific
        (
            r'(?i)aws[-_]?access[-_]?key[-_]?id\s*=\s*[\'\"][^\'"]+[\'\"]',
            "AWS Access Key",
        ),
        (
            r'(?i)aws[-_]?secret[-_]?access[-_]?key\s*=\s*[\'\"][^\'"]+[\'\"]',
            "AWS Secret Key",
        ),
        # Database connection strings
        (r"(?i)jdbc:.*password=\w+", "Database Connection String"),
        (r"(?i)mongodb://[^:]+:[^@]+@", "MongoDB Connection String"),
        # Base64 encoded values (potential certificates/keys)
        (r"(?i)base64:[a-zA-Z0-9+/]{30,}", "Base64 Encoded Value"),
    ]

    findings = []

    for pattern, secret_type in secret_patterns:
        matches = re.finditer(pattern, dockerfile_text)
        for match in matches:
            # Don't include the actual secret value in the result
            # Just report line number and type
            line_number = dockerfile_text[: match.start()].count("\n") + 1
            findings.append(
                {
                    "line": line_number,
                    "type": secret_type,
                    "column": match.start()
                    - dockerfile_text.rfind("\n", 0, match.start()),
                }
            )

    return findings


def recommend_secret_management() -> str:
    """Generate recommendations for secret management."""
    return """
## 🔐 Secret Management Recommendations

Replace hardcoded secrets with these more secure alternatives:

1. **Use Build Arguments (Temporary Secrets):**
   ```dockerfile
   ARG DB_PASSWORD
   ENV DATABASE_PASSWORD=$DB_PASSWORD
   
   # Build with: docker build --build-arg DB_PASSWORD=your_password -t myapp .
   ```

2. **Environment Variables at Runtime:**
   ```dockerfile
   # No secrets in Dockerfile
   
   # Run with: docker run -e DATABASE_PASSWORD=your_password myapp
   # Or with env file: docker run --env-file=.env myapp
   ```

3. **Docker Secrets (Swarm Mode):**
   ```dockerfile
   # In Dockerfile
   RUN --mount=type=secret,id=db_password cat /run/secrets/db_password
   
   # Create secret: docker secret create db_password password.txt
   # Use in compose: 
   # secrets:
   #   db_password:
   #     external: true
   ```

4. **Kubernetes Secrets:**
   ```yaml
   # Define secret
   apiVersion: v1
   kind: Secret
   metadata:
     name: app-secrets
   type: Opaque
   data:
     password: BASE64_ENCODED_PASSWORD
   
   # Mount in deployment
   containers:
     - name: myapp
       env:
         - name: DATABASE_PASSWORD
           valueFrom:
             secretKeyRef:
               name: app-secrets
               key: password
   ```

5. **Secret Management Services:**
   - HashiCorp Vault
   - AWS Secrets Manager
   - Azure Key Vault
   - Google Secret Manager

6. **Secret Detection Tools:**
   ```bash
   # Add to CI/CD
   gitleaks detect --source=./
   trufflehog filesystem --directory=./
   ```
"""


def recommend_image_signing() -> str:
    """Generate recommendations for image signing and security."""
    return """
## 🔏 Image Signing Recommendations

Implement image signing to ensure image authenticity and integrity:

### Docker Content Trust (DCT)

```bash
# Enable Docker Content Trust
export DOCKER_CONTENT_TRUST=1

# Sign the image during push
docker push myregistry.com/myapp:1.0.0

# Verify signed images
docker trust inspect --pretty myregistry.com/myapp:1.0.0
```

### Cosign (Sigstore)

```bash
# Generate a keypair
cosign generate-key-pair

# Sign an image
cosign sign --key cosign.key myregistry.com/myapp:1.0.0

# Verify an image
cosign verify --key cosign.pub myregistry.com/myapp:1.0.0
```

### CI/CD Integration

```yaml
# GitHub Actions example
- name: Install Cosign
  uses: sigstore/cosign-installer@main

- name: Sign the image
  run: |
    cosign sign --key ${COSIGN_KEY} myregistry.com/myapp:${{ github.sha }}
  env:
    COSIGN_KEY: ${{ secrets.COSIGN_KEY }}
    COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}
```

### Policy Enforcement

Configure your Kubernetes cluster to verify signatures with admission controllers:

```yaml
apiVersion: admission.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: verify-image-signatures
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
    - apiGroups: [""]
      apiVersions: ["v1"]
      operations: ["CREATE", "UPDATE"]
      resources: ["pods"]
  validations:
    - expression: "has(object.spec.containers[0].image) && object.spec.containers[0].image.matches('^myregistry.com/.*')"
      message: "Only signed images from myregistry.com are allowed"
```
"""


def recommend_resource_limits() -> str:
    """Generate recommendations for container resource limits."""
    return """
## 🔋 Resource Limits Recommendations

### Docker Run/Compose Resource Limits

```bash
# Set resource limits with docker run
docker run -m 512m --cpus=1.0 myapp:latest

# Docker Compose example
services:
  app:
    image: myapp:latest
    deploy:
      resources:
        limits:
          cpus: '1.0'
          memory: 512M
        reservations:
          cpus: '0.5'
          memory: 256M
```

### Kubernetes Resource Management

```yaml
# Pod specification with resource limits
apiVersion: v1
kind: Pod
metadata:
  name: myapp
spec:
  containers:
  - name: app
    image: myapp:latest
    resources:
      requests:
        memory: "256Mi"
        cpu: "500m"
      limits:
        memory: "512Mi"
        cpu: "1000m"
```

### Resource Limit Best Practices:

1. **Set both requests and limits**
   - Requests: What the container is guaranteed to get
   - Limits: The maximum the container can use

2. **Monitor resource usage to determine proper values**
   ```bash
   # Docker stats
   docker stats myapp
   
   # Kubernetes resource usage
   kubectl top pod myapp
   ```

3. **Consider application-specific settings**
   - JVM memory settings: `-Xmx512m -Xms256m`
   - Node.js memory: `--max-old-space-size=512`
   - Python memory monitoring: `resource` module or memory-profiler

4. **Implement autoscaling based on resource usage**
   ```yaml
   # Kubernetes HPA example
   apiVersion: autoscaling/v2
   kind: HorizontalPodAutoscaler
   metadata:
     name: myapp-hpa
   spec:
     scaleTargetRef:
       apiVersion: apps/v1
       kind: Deployment
       name: myapp
     minReplicas: 1
     maxReplicas: 10
     metrics:
     - type: Resource
       resource:
         name: cpu
         target:
           type: Utilization
           averageUtilization: 80
   ```
"""


def add_dockerfile_healthcheck(dockerfile_text: str) -> str:
    """Add a HEALTHCHECK instruction to a Dockerfile if missing."""
    if "HEALTHCHECK" in dockerfile_text:
        return dockerfile_text  # Already has a healthcheck

    # Try to determine the application type and port
    import re

    # Extract exposed port if available
    exposed_port = "8080"  # Default port
    expose_match = re.search(r"EXPOSE\s+(\d+)", dockerfile_text)
    if expose_match:
        exposed_port = expose_match.group(1)

    # Determine application type
    is_node = any(x in dockerfile_text.lower() for x in ["node", "npm", "yarn"])
    is_python = any(
        x in dockerfile_text.lower() for x in ["python", "pip", "django", "flask"]
    )
    is_java = any(x in dockerfile_text.lower() for x in ["java", "mvn", "gradle"])

    # Create appropriate healthcheck based on app type
    healthcheck = ""
    if is_node:
        healthcheck = f"HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD wget -q -O- http://localhost:{exposed_port}/health || exit 1"
    elif is_python:
        healthcheck = f"HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD curl -f http://localhost:{exposed_port}/health || exit 1"
    elif is_java:
        healthcheck = f"HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD curl -f http://localhost:{exposed_port}/actuator/health || exit 1"
    else:
        healthcheck = f"HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD wget -q -O- http://localhost:{exposed_port}/ || exit 1"

    # Find the right position to add the healthcheck (after EXPOSE, before CMD/ENTRYPOINT)
    cmd_pos = dockerfile_text.find("CMD ")
    entrypoint_pos = dockerfile_text.find("ENTRYPOINT ")

    if cmd_pos == -1 and entrypoint_pos == -1:
        # No CMD or ENTRYPOINT, add to the end
        return dockerfile_text + "\n\n# Add healthcheck\n" + healthcheck

    # Insert before the first CMD or ENTRYPOINT
    insert_pos = min(x for x in [cmd_pos, entrypoint_pos] if x >= 0)
    return (
        dockerfile_text[:insert_pos]
        + "\n# Add healthcheck\n"
        + healthcheck
        + "\n\n"
        + dockerfile_text[insert_pos:]
    )


def enhance_generate_optimization_prompt(
    dockerfile_text: str, preferred_base: str = "original"
) -> str:
    """Enhanced version of generate_optimization_prompt with additional security features."""
    # First call the original function to get the base prompt
    original_prompt = generate_optimization_prompt(dockerfile_text)

    # Add our new recommendations
    distroless_base = suggest_distroless_alternative(dockerfile_text)

    # Detect potential secrets
    secret_findings = detect_hardcoded_secrets(dockerfile_text)
    secrets_section = ""
    if secret_findings:
        secrets_section = "\n## ⚠️ Potential Secrets Detected\n"
        for finding in secret_findings:
            secrets_section += f"- {finding['type']} found at line {finding['line']}\n"

    # Add base image preference to prompt
    base_preference_section = f"""
## 🏗️ Base Image Preference
Preferred base image type: {preferred_base.upper()}

Please respect this preference when suggesting optimizations. This means:
- {"Use Alpine-based images where possible" if preferred_base == "alpine" else ""}
- {"Use slim variants of images where possible" if preferred_base == "slim" else ""}
- {"Use full-featured base images" if preferred_base == "full" else ""}
- {"Keep the original base image type" if preferred_base == "original" else ""}
"""

    additional_sections = f"""
{base_preference_section}

## 🛡️ Advanced Security Recommendations

### Distroless Images
Consider using a distroless base image for production:
```dockerfile
FROM {distroless_base}
```

Distroless images contain only your application and its runtime dependencies, without package managers, shells, or other programs found in standard Linux distributions. This reduces attack surface and image size.

{secrets_section}

### Image Signing
{recommend_image_signing()}

### Secret Management
{recommend_secret_management()}

### SBOM Generation
{recommend_sbom_generation()}

### Resource Limits
{recommend_resource_limits()}

### Vulnerability Scanning
{add_vulnerability_scanning_section()}
"""

    # Add the additional sections before the final validation commands
    validation_cmd_pos = original_prompt.find("## 🚀 Validation Commands")
    if validation_cmd_pos > 0:
        return (
            original_prompt[:validation_cmd_pos]
            + additional_sections
            + original_prompt[validation_cmd_pos:]
        )
    else:
        return original_prompt + additional_sections


def analyze_container_escape_risks(dockerfile_text: str) -> list:
    """Analyze Dockerfile for container escape risks."""
    risks = []

    # Check for privileged mode indicators
    if "--privileged" in dockerfile_text:
        risks.append(
            {
                "severity": "CRITICAL",
                "title": "Container running in privileged mode",
                "description": "Privileged containers can escape isolation and access host resources.",
                "recommendation": "Remove --privileged flag. Use more specific capabilities if needed.",
            }
        )

    # Check for mounting sensitive host directories
    sensitive_mounts = [
        "/proc",
        "/sys",
        "/var/run/docker.sock",
        "docker.sock",
        "/dev",
        "/var",
        "/etc",
    ]

    for mount in sensitive_mounts:
        if f"-v {mount}" in dockerfile_text or f"--volume {mount}" in dockerfile_text:
            risks.append(
                {
                    "severity": "HIGH",
                    "title": f"Mounting sensitive host path {mount}",
                    "description": "Mounting sensitive host paths can lead to container escapes.",
                    "recommendation": f"Avoid mounting {mount}. Use more restricted volumes.",
                }
            )

    # Check for capability additions
    dangerous_caps = ["CAP_SYS_ADMIN", "CAP_NET_ADMIN", "CAP_SYS_PTRACE"]

    for cap in dangerous_caps:
        if (
            f"--cap-add={cap}" in dockerfile_text
            or f"--cap-add {cap}" in dockerfile_text
        ):
            risks.append(
                {
                    "severity": "HIGH",
                    "title": f"Adding dangerous capability {cap}",
                    "description": "This capability could be used to escape the container.",
                    "recommendation": f"Remove {cap} capability. Use more specific permissions.",
                }
            )

    # Check for network=host
    if "--network=host" in dockerfile_text or "--net=host" in dockerfile_text:
        risks.append(
            {
                "severity": "MEDIUM",
                "title": "Using host network mode",
                "description": "Host network mode bypasses container network isolation.",
                "recommendation": "Use the default bridge network or create a custom network.",
            }
        )

    return risks


def cis_docker_benchmark_assessment(dockerfile_text: str) -> dict:
    """Assess Dockerfile against CIS Docker Benchmark."""
    assessment = {"passed": [], "failed": [], "skipped": []}

    # 4.1 Create a user for the container
    if "USER " in dockerfile_text and "USER root" not in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.1",
                "title": "Create a user for the container",
                "description": "Running containers with a non-root user can prevent privilege escalation attacks.",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.1",
                "title": "Create a user for the container",
                "description": "Create a non-root user and use the USER instruction to switch to it.",
            }
        )

    # 4.2 Use trusted base images
    is_known_registry = any(
        registry in dockerfile_text
        for registry in [
            "docker.io",
            "gcr.io",
            "quay.io",
            "mcr.microsoft.com",
            "registry.access.redhat.com",
        ]
    )
    if is_known_registry:
        assessment["passed"].append(
            {
                "id": "4.2",
                "title": "Use trusted base images",
                "description": "Using official or trusted base images reduces security risks.",
            }
        )
    else:
        assessment["skipped"].append(
            {
                "id": "4.2",
                "title": "Use trusted base images",
                "description": "Verify that base images come from trusted sources.",
            }
        )

    # 4.3 Do not install unnecessary packages
    if (
        "apk --no-cache" in dockerfile_text
        or "apt-get --no-install-recommends" in dockerfile_text
    ):
        assessment["passed"].append(
            {
                "id": "4.3",
                "title": "Do not install unnecessary packages",
                "description": "Minimizing installed packages reduces attack surface.",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.3",
                "title": "Do not install unnecessary packages",
                "description": "Use --no-install-recommends for apt or --no-cache for apk.",
            }
        )

    # 4.4 Scan and rebuild images to include security patches
    if "latest" not in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.4",
                "title": "Scan and rebuild images",
                "description": "Using specific versions helps ensure regular rebuilds with security patches.",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.4",
                "title": "Avoid using 'latest' tag",
                "description": "Use specific version tags and implement regular scanning.",
            }
        )

    # 4.5 Enable content trust for Docker
    assessment["skipped"].append(
        {
            "id": "4.5",
            "title": "Enable content trust for Docker",
            "description": "Cannot verify from Dockerfile. Set DOCKER_CONTENT_TRUST=1 in build environment.",
        }
    )

    # 4.6 Add HEALTHCHECK instruction
    if "HEALTHCHECK" in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.6",
                "title": "Add HEALTHCHECK instruction",
                "description": "Healthchecks help ensure container health and proper functioning.",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.6",
                "title": "Add HEALTHCHECK instruction",
                "description": "Add a HEALTHCHECK instruction to detect application failures.",
            }
        )

    # 4.7 Do not use update instructions alone
    if (
        "apt-get update" in dockerfile_text
        and "apt-get update &&" not in dockerfile_text
    ):
        assessment["failed"].append(
            {
                "id": "4.7",
                "title": "Do not use update instructions alone",
                "description": "Combine update and install in single RUN instruction.",
            }
        )
    else:
        assessment["passed"].append(
            {
                "id": "4.7",
                "title": "Do not use update instructions alone",
                "description": "Updates and installs appear to be combined properly.",
            }
        )

    # 4.8 Remove setuid and setgid permissions
    if "chmod -R" in dockerfile_text and "find / -perm" in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.8",
                "title": "Remove setuid and setgid permissions",
                "description": "Removing unnecessary setuid binaries reduces privilege escalation risks.",
            }
        )
    else:
        assessment["skipped"].append(
            {
                "id": "4.8",
                "title": "Remove setuid and setgid permissions",
                "description": "Consider removing setuid/setgid from binaries not required by app.",
            }
        )

    # 4.9 Use COPY instead of ADD
    if "ADD" in dockerfile_text:
        assessment["failed"].append(
            {
                "id": "4.9",
                "title": "Use COPY instead of ADD",
                "description": "COPY is more transparent than ADD and should be preferred.",
            }
        )
    else:
        assessment["passed"].append(
            {
                "id": "4.9",
                "title": "Use COPY instead of ADD",
                "description": "COPY is being used properly instead of ADD.",
            }
        )

    # 4.10 Do not store secrets in Dockerfiles
    has_possible_secrets = any(
        pattern in dockerfile_text.lower()
        for pattern in ["password", "secret", "key", "token", "auth", "cred"]
    )
    if has_possible_secrets:
        assessment["failed"].append(
            {
                "id": "4.10",
                "title": "Do not store secrets in Dockerfiles",
                "description": "Potential secrets found. Use build args, environment variables, or secret management.",
            }
        )
    else:
        assessment["passed"].append(
            {
                "id": "4.10",
                "title": "Do not store secrets in Dockerfiles",
                "description": "No obvious secrets detected in Dockerfile.",
            }
        )

    # 4.11 Install verified packages
    if (
        "apt-get install" in dockerfile_text
        and "--allow-unauthenticated" not in dockerfile_text
    ):
        assessment["passed"].append(
            {
                "id": "4.11",
                "title": "Install verified packages",
                "description": "Package authenticity appears to be verified during installation.",
            }
        )
    else:
        assessment["skipped"].append(
            {
                "id": "4.11",
                "title": "Install verified packages",
                "description": "Ensure packages are verified (avoid --allow-unauthenticated).",
            }
        )

    return assessment


def generate_container_security_best_practices() -> str:
    """Generate container security best practices documentation."""
    return """
## 🔒 Container Security Best Practices

### Runtime Security
- 🛡️ **Run containers with read-only filesystem**
  ```
  docker run --read-only myapp:latest
  ```
  
- 🔐 **Apply seccomp profiles**
  ```
  docker run --security-opt seccomp=profile.json myapp:latest
  ```
  
- 🚫 **Use no-new-privileges flag**
  ```
  docker run --security-opt=no-new-privileges myapp:latest
  ```

### Image Hardening
- 🔍 **Minimize image size**
  - Use multi-stage builds
  - Use distroless or Alpine-based images
  - Remove development tools and documentation
  
- 🧹 **Remove shell access when possible**
  - Use ENTRYPOINT with exec form: `ENTRYPOINT ["executable", "param1"]`
  - Consider distroless images without shell
  
- 📊 **Set resource limits**
  - CPU: `--cpus=1.0`
  - Memory: `-m 512m`
  - PIDs: `--pids-limit=100`

### Supply Chain Security
- 🔏 **Sign and verify images**
  - Use Docker Content Trust or Cosign
  - Verify signatures before deployment
  
- 📜 **Generate and verify SBOMs**
  - Create with Syft or Docker Buildx
  - Verify with Grype
  
- 🔄 **Implement automated base image updates**
  - Use Dependabot or Renovate
  - Set up CI for regular rebuilds

### Configuration Security
- 🌟 **Apply principle of least privilege**
  - Drop capabilities: `--cap-drop=ALL --cap-add=NET_BIND_SERVICE`
  - Use non-root users
  
- 🔧 **Use security contexts in Kubernetes**
  ```yaml
  securityContext:
    runAsNonRoot: true
    allowPrivilegeEscalation: false
    capabilities:
      drop: ["ALL"]
  ```
  
- 🛑 **Implement network policies**
  - Restrict pod-to-pod communication
  - Apply egress filtering

### Operational Security
- 🔍 **Implement runtime security monitoring**
  - Falco for runtime security
  - Sysdig for container monitoring
  
- 📊 **Regular security scanning**
  - Scan images in CI/CD pipeline
  - Scan running containers
  
- 🚨 **Implement monitoring and alerting**
  - Monitor container logs
  - Set up alerts for suspicious activities
"""


def generate_dockerfile_security_report(dockerfile_text: str) -> str:
    """Generate a comprehensive security report for a Dockerfile."""
    escape_risks = analyze_container_escape_risks(dockerfile_text)
    cis_assessment = cis_docker_benchmark_assessment(dockerfile_text)

    # Format escape risks section
    escape_risks_section = ""
    if escape_risks:
        escape_risks_section = "### Container Escape Risks\n\n"
        for risk in escape_risks:
            escape_risks_section += f"- **{risk['severity']}:** {risk['title']}\n"
            escape_risks_section += f"  - {risk['description']}\n"
            escape_risks_section += (
                f"  - **Recommendation:** {risk['recommendation']}\n\n"
            )
    else:
        escape_risks_section = "### Container Escape Risks\n\n✅ No immediate container escape risks detected.\n\n"

    # Format CIS Benchmark section
    cis_section = "### CIS Docker Benchmark Assessment\n\n"

    if cis_assessment["passed"]:
        cis_section += "#### ✅ Passed Checks\n\n"
        for item in cis_assessment["passed"]:
            severity_icon = (
                "🔴"
                if item.get("severity") == "CRITICAL"
                else (
                    "🟠"
                    if item.get("severity") == "HIGH"
                    else "🟡" if item.get("severity") == "MEDIUM" else "🟢"
                )
            )
            cis_section += f"- **{item['id']} {item['title']}** {severity_icon}\n"
            cis_section += f"  - {item['description']}\n\n"

    if cis_assessment["failed"]:
        cis_section += "#### ❌ Failed Checks\n\n"
        for item in cis_assessment["failed"]:
            severity_icon = (
                "🔴"
                if item.get("severity") == "CRITICAL"
                else (
                    "🟠"
                    if item.get("severity") == "HIGH"
                    else "🟡" if item.get("severity") == "MEDIUM" else "🟢"
                )
            )
            cis_section += f"- **{item['id']} {item['title']}** {severity_icon}\n"
            cis_section += f"  - {item['description']}\n\n"

    if cis_assessment["skipped"]:
        cis_section += "#### ⚠️ Manual Review Required\n\n"
        for item in cis_assessment["skipped"]:
            severity_icon = (
                "🔴"
                if item.get("severity") == "CRITICAL"
                else (
                    "🟠"
                    if item.get("severity") == "HIGH"
                    else "🟡" if item.get("severity") == "MEDIUM" else "🟢"
                )
            )
            cis_section += f"- **{item['id']} {item['title']}** {severity_icon}\n"
            cis_section += f"  - {item['description']}\n\n"

    # Calculate overall score
    total_checks = len(cis_assessment["passed"]) + len(cis_assessment["failed"])
    if total_checks > 0:
        score = int((len(cis_assessment["passed"]) / total_checks) * 100)
        score_color = "🟢" if score >= 80 else "🟡" if score >= 60 else "🔴"
        score_section = f"### Security Score: {score_color} {score}%\n\n"
    else:
        score_section = ""

    # Generate remediation examples for failed checks
    remediation_examples = generate_remediation_examples(cis_assessment["failed"])

    # Generate implementation timeline
    implementation_timeline = generate_implementation_timeline(cis_assessment["failed"])

    # Get CI/CD integration examples
    cicd_examples = generate_cicd_integration_examples()

    # Get documentation links
    documentation_links = generate_documentation_links()

    # Combine all sections
    report = f"""
# 🛡️ Dockerfile Security Assessment Report

{score_section}
{escape_risks_section}
{cis_section}

{remediation_examples}

{implementation_timeline}

### 📚 Best Practices

{generate_container_security_best_practices()}

{cicd_examples}

{documentation_links}

### 🔄 Next Steps

1. Address any failed CIS Docker Benchmark checks
2. Implement image signing with Cosign or Docker Content Trust
3. Set up automated vulnerability scanning in CI/CD
4. Generate and verify SBOMs as part of the build process
5. Consider using distroless images for production
"""

    return report


def write_file_with_encoding(file_path, content, encoding="utf-8"):
    """Write content to a file with specific encoding and error handling."""
    try:
        with open(file_path, "w", encoding=encoding) as f:
            f.write(content)
        return True
    except UnicodeEncodeError:
        # If UTF-8 fails, try writing without emoji characters
        try:
            # Simple function to strip emoji characters
            def remove_emojis(text):
                import re

                emoji_pattern = re.compile(
                    "["
                    "\U0001f600-\U0001f64f"  # emoticons
                    "\U0001f300-\U0001f5ff"  # symbols & pictographs
                    "\U0001f680-\U0001f6ff"  # transport & map symbols
                    "\U0001f700-\U0001f77f"  # alchemical symbols
                    "\U0001f780-\U0001f7ff"  # Geometric Shapes
                    "\U0001f800-\U0001f8ff"  # Supplemental Arrows-C
                    "\U0001f900-\U0001f9ff"  # Supplemental Symbols and Pictographs
                    "\U0001fa00-\U0001fa6f"  # Chess Symbols
                    "\U0001fa70-\U0001faff"  # Symbols and Pictographs Extended-A
                    "\U00002702-\U000027b0"  # Dingbats
                    "\U000024c2-\U0001f251"
                    "]+",
                    flags=re.UNICODE,
                )
                return emoji_pattern.sub(r"", text)

            # Replace emoji symbols with text equivalents
            cleaned_content = content
            cleaned_content = cleaned_content.replace("🔒", "[LOCK]")
            cleaned_content = cleaned_content.replace("✅", "[CHECK]")
            cleaned_content = cleaned_content.replace("❌", "[X]")
            cleaned_content = cleaned_content.replace("⚠️", "[WARNING]")
            cleaned_content = cleaned_content.replace("📋", "[REPORT]")
            cleaned_content = cleaned_content.replace("🛡️", "[SHIELD]")
            cleaned_content = cleaned_content.replace("🟢", "[GREEN]")
            cleaned_content = cleaned_content.replace("🟡", "[YELLOW]")
            cleaned_content = cleaned_content.replace("🔴", "[RED]")
            cleaned_content = cleaned_content.replace("📚", "[BOOKS]")
            cleaned_content = cleaned_content.replace("🔄", "[REFRESH]")

            # If there are still emoji characters, remove them
            cleaned_content = remove_emojis(cleaned_content)

            with open(file_path, "w", encoding="ascii", errors="replace") as f:
                f.write(cleaned_content)
            return True
        except Exception as e:
            console.print(
                f"Error writing file with ASCII encoding: {str(e)}", style="red"
            )
            return False
    except Exception as e:
        console.print(f"Error writing file: {str(e)}", style="red")
        return False


# Enhanced Security Assessment Functions
# Add these functions to your Dockerfile Optimizer tool


def generate_cicd_integration_examples() -> str:
    """Generate CI/CD integration examples for Docker security scanning."""
    return """
## 🚀 CI/CD Integration Examples

### GitHub Actions Integration

```yaml
# .github/workflows/docker-security.yml
name: Docker Security Scanning

on:
  push:
    branches: [ main ]
    paths:
      - 'Dockerfile'
      - '.github/workflows/docker-security.yml'
  pull_request:
    branches: [ main ]
    paths:
      - 'Dockerfile'

jobs:
  security-scan:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Build image
        run: docker build -t test-image:${{ github.sha }} .

      - name: Run Trivy vulnerability scanner
        uses: aquasecurity/trivy-action@master
        with:
          image-ref: 'test-image:${{ github.sha }}'
          format: 'sarif'
          output: 'trivy-results.sarif'
          severity: 'CRITICAL,HIGH'
          exit-code: '1'
          ignore-unfixed: true

      - name: Generate SBOM
        run: |
          curl -sSfL https://raw.githubusercontent.com/anchore/syft/main/install.sh | sh -s -- -b /usr/local/bin
          syft test-image:${{ github.sha }} -o json > sbom.json

      - name: Scan SBOM for vulnerabilities
        run: |
          curl -sSfL https://raw.githubusercontent.com/anchore/grype/main/install.sh | sh -s -- -b /usr/local/bin
          grype sbom:./sbom.json --fail-on high

      - name: Sign image (on main branch)
        if: github.ref == 'refs/heads/main' && success()
        uses: sigstore/cosign-installer@main
        with:
          cosign-release: 'v1.13.1'
      - run: |
          echo "${{ secrets.COSIGN_KEY }}" > cosign.key
          cosign sign --key cosign.key test-image:${{ github.sha }}
        env:
          COSIGN_PASSWORD: ${{ secrets.COSIGN_PASSWORD }}
```

### GitLab CI Integration

```yaml
# .gitlab-ci.yml
stages:
  - build
  - scan
  - sign

build:
  stage: build
  image: docker:20.10.16
  services:
    - docker:20.10.16-dind
  script:
    - docker build -t $CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA .
    - docker push $CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA

security_scan:
  stage: scan
  image: aquasec/trivy:latest
  script:
    - trivy image --exit-code 1 --severity HIGH,CRITICAL $CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA

sbom_generation:
  stage: scan
  image: anchore/syft:latest
  script:
    - syft $CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA -o json > sbom.json
    - cat sbom.json | jq '.artifacts | length'
  artifacts:
    paths:
      - sbom.json

image_signing:
  stage: sign
  image: alpine:latest
  script:
    - apk add --no-cache cosign
    - echo "$COSIGN_KEY" > cosign.key
    - cosign sign --key cosign.key $CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA
  only:
    - main
```

### CircleCI Integration

```yaml
# .circleci/config.yml
version: 2.1

orbs:
  docker: circleci/docker@2.1.4

jobs:
  security-scan:
    docker:
      - image: cimg/base:2023.03
    steps:
      - checkout
      - setup_remote_docker:
          version: 20.10.14
      - docker/build:
          image: my-app
          tag: $CIRCLE_SHA1
      - run:
          name: Install Trivy
          command: |
            curl -sfL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | sudo sh -s -- -b /usr/local/bin
      - run:
          name: Scan Docker image
          command: |
            trivy image --exit-code 1 --severity HIGH,CRITICAL my-app:$CIRCLE_SHA1

workflows:
  docker-security:
    jobs:
      - security-scan
```
"""


def generate_documentation_links() -> str:
    """Generate links to Docker security documentation and tools."""
    return """
## 📚 Documentation & Resources

### Official Documentation
- [Docker Security Best Practices](https://docs.docker.com/develop/security-best-practices/)
- [CIS Docker Benchmark](https://www.cisecurity.org/benchmark/docker)
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html)

### Security Tools
- [Trivy Scanner](https://github.com/aquasecurity/trivy) - Comprehensive container vulnerability scanner
- [Cosign](https://github.com/sigstore/cosign) - Container signing, verification, and storage
- [Syft](https://github.com/anchore/syft) - SBOM generator for containers
- [Grype](https://github.com/anchore/grype) - Vulnerability scanner for SBOM files
- [Docker Scout](https://docs.docker.com/scout/) - Official Docker vulnerability scanning
- [Falco](https://falco.org/) - Cloud-native runtime security

### Distroless Images
- [Google Distroless](https://github.com/GoogleContainerTools/distroless) - Base image alternatives
- [Chainguard Images](https://www.chainguard.dev/chainguard-images) - Minimal, secure base images

### Secrets Management
- [HashiCorp Vault](https://www.vaultproject.io/) - Secrets management
- [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/) - Cloud-based secrets management
- [GitLeaks](https://github.com/zricethezav/gitleaks) - Secret scanning for git repositories
"""


def generate_remediation_examples(failed_checks) -> str:
    """Generate specific Dockerfile remediation examples based on failed checks."""
    remediation_examples = "## 🛠️ Remediation Examples\n\n"

    for check in failed_checks:
        if check["id"] == "4.1":  # Create a user
            remediation_examples += """### Non-Root User (4.1)
```dockerfile
# For Debian/Ubuntu-based images
RUN groupadd -r appgroup && useradd -r -g appgroup appuser
USER appuser

# For Alpine-based images
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
USER appuser
```
"""
        elif check["id"] == "4.3":  # Unnecessary packages
            remediation_examples += """### Minimize Installed Packages (4.3)
```dockerfile
# For Debian/Ubuntu-based images
RUN apt-get update && apt-get install --no-install-recommends -y package1 package2 \\
    && rm -rf /var/lib/apt/lists/*

# For Alpine-based images
RUN apk add --no-cache package1 package2
```
"""
        elif check["id"] == "4.4":  # Avoid latest
            remediation_examples += """### Use Specific Image Tags (4.4)
```dockerfile
# Instead of
FROM node:latest

# Use specific version
FROM node:18.15.0-alpine3.16
```
"""
        elif check["id"] == "4.6":  # Healthcheck
            remediation_examples += """### Add Healthcheck (4.6)
```dockerfile
# For web services
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \\
  CMD wget -q -O- http://localhost:8080/health || exit 1

# For non-web services
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \\
  CMD pgrep -f "main process" || exit 1
```
"""
        elif check["id"] == "4.7":  # Update instructions
            remediation_examples += """### Combine Update Instructions (4.7)
```dockerfile
# Instead of
RUN apt-get update
RUN apt-get install -y package1 package2

# Use this combined form
RUN apt-get update && apt-get install -y package1 package2 \\
    && rm -rf /var/lib/apt/lists/*
```
"""
        elif check["id"] == "4.8":  # setuid/setgid
            remediation_examples += """### Remove unnecessary setuid binaries (4.8)
```dockerfile
RUN find / -perm /6000 -type f -exec chmod a-s {} \\; || true
```
"""
        elif check["id"] == "4.9":  # COPY instead of ADD
            remediation_examples += """### Use COPY instead of ADD (4.9)
```dockerfile
# Instead of
ADD https://example.com/file.tar.gz /tmp/
RUN tar -xzf /tmp/file.tar.gz -C /app

# Use this
RUN wget -O /tmp/file.tar.gz https://example.com/file.tar.gz \\
    && tar -xzf /tmp/file.tar.gz -C /app \\
    && rm /tmp/file.tar.gz

# Or for local files, instead of
ADD . /app

# Use
COPY . /app
```
"""
        elif check["id"] == "4.10":  # Secrets
            remediation_examples += """### Avoid storing secrets (4.10)
```dockerfile
# Instead of
ENV API_KEY="secret-key-value"

# Use build arguments (for build-time only)
ARG API_KEY
RUN ./setup.sh $API_KEY

# Or use runtime environment variables (preferred)
# In Dockerfile - don't set a value:
ENV API_KEY=""
# Then at runtime:
# docker run -e API_KEY=secret-value myimage
```
"""

    return remediation_examples


def generate_implementation_timeline(failed_checks) -> str:
    """Generate timeline recommendations for implementing security fixes."""
    # Categorize the failed checks by severity
    critical = [c for c in failed_checks if c["severity"] == "CRITICAL"]
    high = [c for c in failed_checks if c["severity"] == "HIGH"]
    medium = [c for c in failed_checks if c["severity"] == "MEDIUM"]
    low = [c for c in failed_checks if c["severity"] == "LOW"]

    timeline = "## ⏱️ Implementation Timeline\n\n"

    if critical or high:
        timeline += "### Immediate (Next 24-48 hours)\n\n"
        for check in critical + high:
            timeline += f"- **{check['id']} {check['title']}** ({check['severity']})\n"
        timeline += "\n"

    if medium:
        timeline += "### Short-term (Next 1-2 weeks)\n\n"
        for check in medium:
            timeline += f"- **{check['id']} {check['title']}** ({check['severity']})\n"
        timeline += "\n"

    if low:
        timeline += "### Mid-term (Next 2-4 weeks)\n\n"
        for check in low:
            timeline += f"- **{check['id']} {check['title']}** ({check['severity']})\n"
        timeline += "\n"

    timeline += """### Long-term (Next 1-3 months)
    
- Implement automated image scanning in CI/CD pipeline
- Set up container signing workflow
- Generate and verify SBOMs in build process
- Implement runtime container security monitoring
- Establish container security policy document
"""

    return timeline


def cis_docker_benchmark_assessment(dockerfile_text: str) -> dict:
    """Assess Dockerfile against CIS Docker Benchmark."""
    assessment = {"passed": [], "failed": [], "skipped": []}

    # 4.1 Create a user for the container
    if "USER " in dockerfile_text and "USER root" not in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.1",
                "title": "Create a user for the container",
                "description": "Running containers with a non-root user can prevent privilege escalation attacks.",
                "severity": "HIGH",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.1",
                "title": "Create a user for the container",
                "description": "Create a non-root user and use the USER instruction to switch to it.",
                "severity": "HIGH",
            }
        )

    # 4.2 Use trusted base images
    is_known_registry = any(
        registry in dockerfile_text
        for registry in [
            "docker.io",
            "gcr.io",
            "quay.io",
            "mcr.microsoft.com",
            "registry.access.redhat.com",
        ]
    )
    if is_known_registry:
        assessment["passed"].append(
            {
                "id": "4.2",
                "title": "Use trusted base images",
                "description": "Using official or trusted base images reduces security risks.",
                "severity": "MEDIUM",
            }
        )
    else:
        assessment["skipped"].append(
            {
                "id": "4.2",
                "title": "Use trusted base images",
                "description": "Verify that base images come from trusted sources.",
                "severity": "MEDIUM",
            }
        )

    # 4.3 Do not install unnecessary packages
    if (
        "apk --no-cache" in dockerfile_text
        or "apt-get --no-install-recommends" in dockerfile_text
    ):
        assessment["passed"].append(
            {
                "id": "4.3",
                "title": "Do not install unnecessary packages",
                "description": "Minimizing installed packages reduces attack surface.",
                "severity": "MEDIUM",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.3",
                "title": "Do not install unnecessary packages",
                "description": "Use --no-install-recommends for apt or --no-cache for apk.",
                "severity": "MEDIUM",
            }
        )

    # 4.4 Scan and rebuild images to include security patches
    if "latest" not in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.4",
                "title": "Scan and rebuild images",
                "description": "Using specific versions helps ensure regular rebuilds with security patches.",
                "severity": "HIGH",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.4",
                "title": "Avoid using 'latest' tag",
                "description": "Use specific version tags and implement regular scanning.",
                "severity": "HIGH",
            }
        )

    # 4.5 Enable content trust for Docker
    assessment["skipped"].append(
        {
            "id": "4.5",
            "title": "Enable content trust for Docker",
            "description": "Cannot verify from Dockerfile. Set DOCKER_CONTENT_TRUST=1 in build environment.",
            "severity": "MEDIUM",
        }
    )

    # 4.6 Add HEALTHCHECK instruction
    if "HEALTHCHECK" in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.6",
                "title": "Add HEALTHCHECK instruction",
                "description": "Healthchecks help ensure container health and proper functioning.",
                "severity": "MEDIUM",
            }
        )
    else:
        assessment["failed"].append(
            {
                "id": "4.6",
                "title": "Add HEALTHCHECK instruction",
                "description": "Add a HEALTHCHECK instruction to detect application failures.",
                "severity": "MEDIUM",
            }
        )

    # 4.7 Do not use update instructions alone
    if (
        "apt-get update" in dockerfile_text
        and "apt-get update &&" not in dockerfile_text
    ):
        assessment["failed"].append(
            {
                "id": "4.7",
                "title": "Do not use update instructions alone",
                "description": "Combine update and install in single RUN instruction.",
                "severity": "LOW",
            }
        )
    else:
        assessment["passed"].append(
            {
                "id": "4.7",
                "title": "Do not use update instructions alone",
                "description": "Updates and installs appear to be combined properly.",
                "severity": "LOW",
            }
        )

    # 4.8 Remove setuid and setgid permissions
    if "chmod -R" in dockerfile_text and "find / -perm" in dockerfile_text:
        assessment["passed"].append(
            {
                "id": "4.8",
                "title": "Remove setuid and setgid permissions",
                "description": "Removing unnecessary setuid binaries reduces privilege escalation risks.",
                "severity": "HIGH",
            }
        )
    else:
        assessment["skipped"].append(
            {
                "id": "4.8",
                "title": "Remove setuid and setgid permissions",
                "description": "Consider removing setuid/setgid from binaries not required by app.",
                "severity": "HIGH",
            }
        )

    # 4.9 Use COPY instead of ADD
    if "ADD" in dockerfile_text:
        assessment["failed"].append(
            {
                "id": "4.9",
                "title": "Use COPY instead of ADD",
                "description": "COPY is more transparent than ADD and should be preferred.",
                "severity": "LOW",
            }
        )
    else:
        assessment["passed"].append(
            {
                "id": "4.9",
                "title": "Use COPY instead of ADD",
                "description": "COPY is being used properly instead of ADD.",
                "severity": "LOW",
            }
        )

    # 4.10 Do not store secrets in Dockerfiles
    has_possible_secrets = any(
        pattern in dockerfile_text.lower()
        for pattern in ["password", "secret", "key", "token", "auth", "cred"]
    )
    if has_possible_secrets:
        assessment["failed"].append(
            {
                "id": "4.10",
                "title": "Do not store secrets in Dockerfiles",
                "description": "Potential secrets found. Use build args, environment variables, or secret management.",
                "severity": "CRITICAL",
            }
        )
    else:
        assessment["passed"].append(
            {
                "id": "4.10",
                "title": "Do not store secrets in Dockerfiles",
                "description": "No obvious secrets detected in Dockerfile.",
                "severity": "CRITICAL",
            }
        )

    # 4.11 Install verified packages
    if (
        "apt-get install" in dockerfile_text
        and "--allow-unauthenticated" not in dockerfile_text
    ):
        assessment["passed"].append(
            {
                "id": "4.11",
                "title": "Install verified packages",
                "description": "Package authenticity appears to be verified during installation.",
                "severity": "MEDIUM",
            }
        )
    else:
        assessment["skipped"].append(
            {
                "id": "4.11",
                "title": "Install verified packages",
                "description": "Ensure packages are verified (avoid --allow-unauthenticated).",
                "severity": "MEDIUM",
            }
        )

    return assessment


def main():
    """Enhanced main function with advanced security analysis features."""
    try:
        dockerfile_path = input("Enter path to Dockerfile: ").strip()

        if not os.path.exists(dockerfile_path):
            console.print("❌ File not found.", style="bold red")
            exit(1)

        with open(dockerfile_path, "r", encoding="utf-8") as f:
            dockerfile_text = f.read()

        # ADD THIS: Get base image preference
        console.print("\n🔧 Base Image Preference", style="bold cyan")
        base_options = ["alpine", "slim", "full", "original"]
        console.print(f"Options: {', '.join(base_options)}", style="cyan")
        preferred_base = (
            input("Preferred base image type (default: original): ").strip().lower()
            or "original"
        )
        if preferred_base not in base_options:
            console.print(
                f"⚠️ Invalid option. Using 'original' instead.", style="yellow"
            )
            preferred_base = "original"

        console.print(
            "\n🔧 Advanced Dockerfile Optimization and Security Analysis 🔧",
            style="bold cyan",
        )
        console.print("1️⃣ Validating Dockerfile...", style="cyan")

        # Prompt for .dockerignore creation
        generate_dockerignore(os.path.dirname(dockerfile_path), prompt_user=True)

        # Calculate metrics before optimization
        original_size, optimized_size = enhanced_image_size_estimation(dockerfile_text)
        original_time, optimized_time = enhanced_build_time_estimation(dockerfile_text)
        security_checks = generate_security_checklist(dockerfile_text)
        env_analysis = analyze_environment_differences(dockerfile_text)

        # ADD: Security analysis features
        escape_risks = analyze_container_escape_risks(dockerfile_text)
        if escape_risks:
            console.print("\n⚠️ Container escape risks detected:", style="bold red")
            for risk in escape_risks:
                console.print(f"  - {risk['severity']}: {risk['title']}", style="red")
                console.print(f"    {risk['description']}", style="dim")
                console.print(
                    f"    Recommendation: {risk['recommendation']}", style="yellow"
                )

        # ADD: CIS benchmark assessment
        cis_assessment = cis_docker_benchmark_assessment(dockerfile_text)
        passed_count = len(cis_assessment["passed"])
        failed_count = len(cis_assessment["failed"])
        total_assessed = passed_count + failed_count
        if total_assessed > 0:
            compliance_score = int((passed_count / total_assessed) * 100)
            console.print(
                f"\n🔒 CIS Docker Benchmark: {compliance_score}% compliance",
                style="bold cyan",
            )
            console.print(f"  - ✅ Passed: {passed_count} checks", style="green")
            console.print(f"  - ❌ Failed: {failed_count} checks", style="red")
            console.print(
                f"  - ⚠️ Manual review needed: {len(cis_assessment['skipped'])} checks",
                style="yellow",
            )

        # Check for possible secrets (new feature)
        secret_findings = detect_hardcoded_secrets(dockerfile_text)
        if secret_findings:
            console.print("\n⚠️ Potential secrets detected:", style="bold yellow")
            for finding in secret_findings:
                console.print(
                    f"  - {finding['type']} at line {finding['line']}", style="yellow"
                )

            console.print(
                "\n🔒 Always use secure methods to handle secrets:", style="bold yellow"
            )
            console.print("  - Environment variables at runtime", style="yellow")
            console.print("  - Docker secrets or Kubernetes secrets", style="yellow")
            console.print(
                "  - External secret managers (Vault, AWS Secrets Manager, etc.)",
                style="yellow",
            )

        # Check for missing healthcheck (new feature)
        if "HEALTHCHECK" not in dockerfile_text:
            console.print("\n💡 No HEALTHCHECK instruction found.", style="yellow")
            response = (
                input("Would you like to add a healthcheck instruction? (y/n): ")
                .strip()
                .lower()
            )
            if response == "y":
                dockerfile_text = add_dockerfile_healthcheck(dockerfile_text)
                console.print("✅ HEALTHCHECK instruction added.", style="green")
                if write_file_with_encoding(dockerfile_path, dockerfile_text):
                    console.print(
                        "✅ Dockerfile updated with HEALTHCHECK.", style="green"
                    )
                else:
                    console.print(
                        "⚠️ Could not update Dockerfile with HEALTHCHECK", style="yellow"
                    )

        # ADD: Ask if user wants a detailed security report
        console.print(
            "\n📋 Would you like to generate an enhanced security report? The report includes:",
            style="bold cyan",
        )
        console.print("  - Severity classifications for security issues", style="cyan")
        console.print("  - Implementation timeline recommendations", style="cyan")
        console.print("  - Dockerfile-specific remediation examples", style="cyan")
        console.print("  - CI/CD integration examples", style="cyan")
        console.print("  - Links to security documentation and tools", style="cyan")

        security_report_response = (
            input("Generate enhanced security report? (y/n): ").strip().lower()
        )
        if security_report_response == "y":
            security_report = generate_dockerfile_security_report(dockerfile_text)
            security_report_path = os.path.join(
                os.path.dirname(dockerfile_path), "dockerfile_security_report.md"
            )
            if write_file_with_encoding(security_report_path, security_report):
                console.print(
                    f"✅ Enhanced security report generated at {security_report_path}",
                    style="green",
                )
            else:
                console.print(
                    "⚠️ Could not write security report due to encoding issues",
                    style="yellow",
                )

        console.print("2️⃣ Analyzing with AI...", style="cyan")
        # Use enhanced prompt generator
        prompt = enhance_generate_optimization_prompt(dockerfile_text, preferred_base)
        result = optimize_dockerfile(dockerfile_text, prompt)

        # Display summary metrics in a nice table
        display_summary_table(
            dockerfile_path,
            original_size,
            optimized_size,
            original_time,
            optimized_time,
            security_checks,
            env_analysis,
        )

        console.print("3️⃣ Optimization Complete!", style="bold green")
        md = Markdown(result)
        console.print(md)

        # Extract optimized Dockerfile content
        optimized_dockerfile = extract_optimized_dockerfile(result)
        if optimized_dockerfile:
            apply_optimized_dockerfile(dockerfile_path, optimized_dockerfile)
        else:
            console.print(
                "⚠️ Could not extract optimized Dockerfile from the result.",
                style="yellow",
            )

        # Ask if the user wants to add distroless recommendation (new feature)
        distroless_base = suggest_distroless_alternative(dockerfile_text)
        console.print(f"\n💡 Distroless Alternative: {distroless_base}", style="yellow")
        console.print(
            "Distroless images reduce attack surface and improve security.",
            style="yellow",
        )

        # Offer to add vulnerability scanning comments to Dockerfile (new feature)
        console.print(
            "\n💡 Add vulnerability scanning recommendations to your Dockerfile?",
            style="yellow",
        )
        scan_response = (
            input("Add vulnerability scanning comments? (y/n): ").strip().lower()
        )
        if scan_response == "y":
            with open(dockerfile_path, "r") as f:
                current_dockerfile = f.read()

            updated_dockerfile = integrate_vulnerability_scanning(current_dockerfile)
            if write_file_with_encoding(dockerfile_path, updated_dockerfile):
                console.print(
                    "✅ Vulnerability scanning comments added.", style="green"
                )
            else:
                console.print(
                    "⚠️ Could not add vulnerability scanning comments", style="yellow"
                )

        # ADD: Final security recommendations
        console.print("\n🚀 Security recommendations:", style="bold green")
        console.print(
            "  - Implement image signing with Docker Content Trust or Cosign",
            style="green",
        )
        console.print(
            "  - Set up automated vulnerability scanning in CI/CD", style="green"
        )
        console.print(
            "  - Generate and verify SBOMs as part of the build process", style="green"
        )
        console.print(
            "  - Consider using distroless images for production", style="green"
        )

    except Exception as e:
        console.print(f"\n⚠️ Optimization Failed: {str(e)}", style="bold red")
        exit(1)


# Replace the existing if __name__ == "__main__": block with this enhanced version
if __name__ == "__main__":
    main()
