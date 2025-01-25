# Flask Application


## Development

Development server:

```bash
flask --app app run --debug
```

or

```bash
python app.py
```

Lock dependences before release: 

```bash
 uv pip compile pyproject.toml -o requirements.txt
 ```
 
