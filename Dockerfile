FROM python:3.12-slim

COPY scripts/install_kiwix_tools.py /tmp/install_kiwix_tools.py
RUN python /tmp/install_kiwix_tools.py && rm /tmp/install_kiwix_tools.py && kiwix-serve --version

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --frozen --no-dev

EXPOSE 8080

# One hypercorn process (no worker supervisor). The manager starts and supervises kiwix-serve itself.
CMD ["/app/.venv/bin/hypercorn", "kiwix_manager.app:app", "--workers", "0", "--bind", "0.0.0.0:8080"]
