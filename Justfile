set shell := ["bash", "-cu"]

default:
	@just --list

help:
	@just --list

sync mode="":
	bash scripts/sync-to-global.sh {{mode}}

sync-dry:
	bash scripts/sync-to-global.sh --dry-run

check-shell:
	find scripts -name '*.sh' -exec bash -n {} +

