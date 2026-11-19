.PHONY: models videos test test-fast smoke analyze deploy edge scenarios-quick scenarios-full dash
PY ?= python3
models:          ; PYTHONPATH=src $(PY) -m vidp fetch-models --dir models --animals data/animals
videos: models   ; PYTHONPATH=src $(PY) -m vidp synth --out data/videos --animals data/animals --count 8 --duration 20
test-fast:       ; PYTHONPATH=src MODELS_DIR=models $(PY) -m pytest -q tests/test_core.py tests/test_cloud.py
test: models     ; PYTHONPATH=src MODELS_DIR=models $(PY) -m pytest -q tests
smoke: videos    ; PYTHONPATH=src MODELS_DIR=models $(PY) scripts/dev_smoke.py --out /tmp/vidp_smoke --videos data/videos --models models
analyze:         ; $(PY) scripts/analyze.py
deploy:          ; ./scripts/deploy.sh all
edge:            ; ./scripts/deploy.sh edge
scenarios-quick: ; $(PY) scripts/run_scenarios.py --preset quick
scenarios-full:  ; $(PY) scripts/run_scenarios.py --preset full
dash:            ; $(PY) edge/monitoring/gen_dashboard.py > edge/monitoring/grafana/dashboards/vidp.json
