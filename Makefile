PATH  := $(PATH):$(HOME)/.local/bin
SHELL := env PATH=$(PATH) /bin/bash


build:
	uv sync --all-extras

compile:
	uv run protobunny generate
	make format

format:
	uv run ruff check . --select I --fix
	uv run ruff format .

lint:
	uv run ruff check . --diff
	uv run ruff format . --check --diff
