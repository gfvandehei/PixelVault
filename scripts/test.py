import os

from dotenv import load_dotenv
ENV_FILE = os.getenv("ENV_FILE", None)
load_dotenv(dotenv_path=ENV_FILE)

from pixelvault import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', 5000)), debug=True)
