PATH  := $(PATH):$(HOME)/.local/bin
SHELL := env PATH=$(PATH) /bin/bash

help : Makefile
	@sed -n 's/^##//p' $<

.PHONY: build compile
build:
	uv sync --all-extas

compile:
	uv run protobunny generate
	make format

.PHONY: format
format:
	uv run ruff check . --select I --fix
	uv run ruff format .

.PHONY: lint
lint:
	uv run ruff check . --diff
	uv run ruff format . --check --diff
