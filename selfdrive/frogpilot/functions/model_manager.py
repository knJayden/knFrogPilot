import os
import stat
import urllib.request

from openpilot.common.params import Params

DEFAULT_MODEL = "los-angeles"

VERSION = 'v1'
DOWNLOAD_URL = "https://github.com/FrogAi/FrogPilot-Resources/releases/download"
REPOSITORY_URL = f"https://raw.githubusercontent.com/FrogAi/FrogPilot-Resources/master"
MODELS_PATH = '/data/models'

def delete_deprecated_models(params):
  available_models = params.get("AvailableModels", encoding='utf-8').split(',')

  for f in os.listdir(MODELS_PATH):
    if f.endswith('.thneed') and f.split('.')[0] not in available_models:
      os.remove(os.path.join(MODELS_PATH, f))

def download_model(params):
  selected_model = params.get("Model", encoding='utf-8')
  download_url = f"{DOWNLOAD_URL}/{selected_model}/{selected_model}.thneed"

  if not os.path.exists(MODELS_PATH):
    os.makedirs(MODELS_PATH)

  model_file_path = os.path.join(MODELS_PATH, f"{selected_model}.thneed")

  with urllib.request.urlopen(download_url) as f, open(model_file_path, 'wb') as output:
    output.write(f.read())
    os.fsync(output.fileno())

  current_permissions = stat.S_IMODE(os.lstat(model_file_path).st_mode)
  os.chmod(model_file_path, current_permissions | stat.S_IEXEC)

def populate_models(params):
  model_names_url = f"{REPOSITORY_URL}/model_names_{VERSION}.txt"

  with urllib.request.urlopen(model_names_url) as response:
    output = response.read().decode('utf-8')

  params.put("AvailableModels", ','.join([line.split(' - ')[0] for line in output.split('\n') if ' - ' in line]))
  params.put("AvailableModelsNames", ','.join([line.split(' - ')[1] for line in output.split('\n') if ' - ' in line]))

def main():
  params = Params()

  download_model(params)
  populate_models(params)
  delete_deprecated_models(params)

if __name__ == "__main__":
  main()
