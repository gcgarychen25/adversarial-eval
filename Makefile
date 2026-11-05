.PHONY: install bootstrap validate test dry-agent help
install:
	python -m pip install -e '.[data,test]'
bootstrap:
	python -m adversarial.real_eval bootstrap --root work
validate:
	python -m adversarial.real_eval validate --root work
test:
	python -m pytest -q tests/
dry-agent:
	python -m adversarial cheat-agent --task 1012 --dry-run
help:
	python -m adversarial cheat-agent --help
	python -m adversarial cheat-review --help
