.PHONY: test demo image

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

demo:
	PYTHONPATH=src python3 -m evoforge demo --output runs/demo --rounds 9 --ablations --seal

image:
	docker build -t evoforge-runtime:local -f runtime/Dockerfile runtime
