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


def generate_env_optimized_dockerfile(dockerfile_text: str) -> str:
    """Generate environment-optimized Dockerfile using ARG and multi-stage builds.

    This creates a template that uses build args to create either dev or prod builds.
    """
    # Analyze the current Dockerfile
    base_image_match = re.search(r"FROM\s+([^\s]+)", dockerfile_text)
    base_image = base_image_match.group(1) if base_image_match else "alpine:3.16"

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

    # Create optimized template based on application type
    if is_node:
        return f"""# Optimized Dockerfile with dev/prod environments
# Usage:
# Development: docker build --build-arg ENV=development -t myapp:dev .
# Production: docker build --build-arg ENV=production -t myapp:prod .

# Build stage
FROM node:16-alpine AS builder
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
FROM node:16-alpine AS production
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
      apk add --no-cache vim curl; \\
    fi

# Create non-root user for security
RUN addgroup -S appgroup && adduser -S appuser -G appgroup
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
FROM python:3.9-slim AS builder
ARG ENV=production
WORKDIR {workdir}

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \\
    gcc \\
    && rm -rf /var/lib/apt/lists/*

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
FROM python:3.9-slim AS production
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
      apt-get update && apt-get install -y --no-install-recommends \\
        vim \\
        curl \\
        && rm -rf /var/lib/apt/lists/*; \\
    fi

# Create non-root user for security
RUN groupadd -r appgroup && useradd -r -g appgroup appuser
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
FROM {base_image} AS builder
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
FROM {base_image} AS production
ARG ENV=production
WORKDIR {workdir}

# Copy from builder stage
COPY --from=builder {workdir} ./

# Add development tools if in dev environment
RUN if [ "$ENV" = "development" ]; then \\
      echo "Installing development tools"; \\
    fi

# Create non-root user for security
RUN adduser -D appuser
USER appuser

# Expose port
EXPOSE {expose_port}

# Start the application
CMD ["echo", "Application started"]
"""


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


def optimize_dockerfile(dockerfile_text: str) -> str:
    """Optimize Dockerfile using an AI provider (Gemini, OpenAI, Claude, or Perplexity)."""
    # Initial validation
    is_valid, issues = validate_dockerfile(dockerfile_text)
    if not is_valid:
        console.print("\n❌ Critical issues found:", style="bold red")
        for issue in issues["critical"]:
            console.print(f"  - {issue}", style="red")
        raise ValueError("Dockerfile validation failed")

    # Generate optimized prompt
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


if __name__ == "__main__":
    try:
        dockerfile_path = input("Enter path to Dockerfile: ").strip()

        if not os.path.exists(dockerfile_path):
            console.print("❌ File not found.", style="bold red")
            exit(1)

        with open(dockerfile_path, "r") as f:
            dockerfile_text = f.read()

        console.print(
            "\n🔧 Advanced Dockerfile Optimization Process 🔧", style="bold cyan"
        )
        console.print("1️⃣ Validating Dockerfile...", style="cyan")

        # Prompt for .dockerignore creation
        generate_dockerignore(os.path.dirname(dockerfile_path), prompt_user=True)

        # Calculate metrics before optimization
        original_size, optimized_size = enhanced_image_size_estimation(dockerfile_text)
        original_time, optimized_time = enhanced_build_time_estimation(dockerfile_text)
        security_checks = generate_security_checklist(dockerfile_text)

        console.print("2️⃣ Analyzing with Google Gemini AI...", style="cyan")
        result = optimize_dockerfile(dockerfile_text)

        # Display summary metrics in a nice table
        display_summary_table(
            dockerfile_path,
            original_size,
            optimized_size,
            original_time,
            optimized_time,
            security_checks,
        )

        console.print("3️⃣ Optimization Complete!", style="bold green")
        md = Markdown(result)
        console.print(md)

        # Extract optimized Dockerfile content
        optimized_dockerfile = extract_optimized_dockerfile(result)
        if optimized_dockerfile:
            # Ask user if they want to apply the optimized Dockerfile
            apply_optimized_dockerfile(dockerfile_path, optimized_dockerfile)
        else:
            console.print(
                "⚠️ Could not extract optimized Dockerfile from the result.",
                style="yellow",
            )

    except Exception as e:
        console.print(f"\n⚠️ Optimization Failed: {str(e)}", style="bold red")
        exit(1)
