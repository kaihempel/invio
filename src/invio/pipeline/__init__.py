"""Run orchestration: the composition root above ``invio.graph``.

``invio.pipeline`` wires the concrete adapters (HTTP client, source adapters, LLM providers,
e-mail delivery, schedule computation) into the research graph and runs one job
(:func:`invio.pipeline.run.run_job`). ``invio.graph`` itself stays free of ``notify`` and
``scheduling`` (research R2); only ``invio.cli`` may import this package.
"""
