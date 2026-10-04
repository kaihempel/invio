# Model registry files

Every `<provider>.yaml` in this directory declares the models of one LLM provider. Only
`*.yaml` files are loaded; everything else (like this README) is ignored.

```yaml
schema_version: 1          # must be 1
provider: <provider>       # must equal the file name without ".yaml"
models:                    # mapping, may be empty
  <model-id>:
    input_price_per_mtok: <number >= 0>    # USD per 1,000,000 input tokens
    output_price_per_mtok: <number >= 0>   # USD per 1,000,000 output tokens
    context_window: <integer > 0>          # tokens
```

Files are parsed strictly: unknown keys, missing fields, invalid values, a `provider` that
differs from the file name and a model id defined in two files are all errors.
