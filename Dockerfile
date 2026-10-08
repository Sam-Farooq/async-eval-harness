FROM python:3.11.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONHASHSEED=0 \
    TZ=UTC \
    LC_ALL=C

# patch(1) is not in slim and verify.py shells out to it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin runner
WORKDIR /bench

# Dependencies ahead of the source so a change to task_queue.py does not
# reinstall pytest. The image is rebuilt once per candidate patch.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[dev]"

COPY --chown=runner:runner src/ ./src/
COPY --chown=runner:runner tests/ ./tests/
COPY --chown=runner:runner verify.py manifest.json solution.patch ./

RUN chown -R runner:runner /bench
USER runner

# The default run is the baseline, which is expected to fail: four
# nine fail_to_pass tests red, exit 1. That is the correct state of an unsolved
# instance, so do not treat a non-zero exit here as a broken image.
ENTRYPOINT ["python", "verify.py"]
CMD ["--no-patch"]
