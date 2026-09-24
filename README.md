# CamSecMon — monitor local em Python

A câmera iM5 SC fornece o vídeo; o programa identifica passagens pela linha da entrada, salva a foto da pessoa e tenta relacionar a saída à entrada pela cor da roupa. O painel mostra horários, duração e saídas que precisam de confirmação. Uma nova entrada gera outra visita. Não faz reconhecimento facial nem identifica nomes.

## Instalação (Windows, PowerShell)

Instale Python 3.11 ou 3.12. Na pasta `camsecmon`:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Se o comando `py -3.11` não existir, use `py -3.12`. Na primeira execução, o Ultralytics baixa o modelo YOLO (`yolo11n.pt`); é necessária conexão com a internet somente para esse download e para instalar as dependências.

## Testar com um vídeo, antes da câmera

Grave um vídeo que mostre a entrada e saída com a câmera fixa, e use:

```powershell
$env:VIDEO_FILE="C:\videos\entrada.mp4"
$env:LINE_X="0.50"
$env:ENTRY_DIRECTION="left_to_right"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Abra http://127.0.0.1:8000 . `LINE_X` indica onde fica a linha vertical de cruzamento: 0.50 é o centro, 0.35 fica a 35% da largura. `ENTRY_DIRECTION` pode ser `left_to_right` ou `right_to_left`. O vídeo precisa mostrar a pessoa atravessando a linha nas duas direções. Na reprodução de arquivo, os horários registrados seguem a hora da execução, não a hora original da gravação.

## Conectar à iM5 SC

Ative o vídeo RTSP/ONVIF nas configurações da câmera, conforme a versão do firmware e do aplicativo Mibo Smart. Confira IP, usuário, senha e endereço RTSP usando o manual da câmera. Na mesma rede local, substitua abaixo pelos dados reais:

```powershell
Remove-Item Env:VIDEO_FILE -ErrorAction SilentlyContinue
$env:CAMERA_RTSP="rtsp://USUARIO:SENHA@IP_DA_CAMERA:554/CAMINHO_DO_STREAM"
uvicorn app:app --host 127.0.0.1 --port 8000
```

**Não compartilhe a URL real**: ela contém a senha da câmera. Não coloque a câmera diretamente na internet. Preferencialmente use uma conta de leitura dedicada. Ajuste `LINE_X` e `ENTRY_DIRECTION` ao ângulo da porta.

## Dados e limites

- As fotos e o banco SQLite ficam em `data/`, na pasta do projeto. Faça backup dessa pasta e estabeleça um prazo de retenção apropriado. O sistema não faz limpeza automática.
- O painel atende apenas em `127.0.0.1`: não há login para acesso de outros computadores.
- A correspondência por roupa é experimental: uniformes iguais, troca de roupa, iluminação e pessoas que passam lado a lado podem causar erros. Revisão humana é necessária para uso individual.
- O detector pode perder entradas e saídas, sobretudo quando a câmera não vê toda a porta. Não use este protótipo para ponto, cobrança ou decisões disciplinares.
- Para encerrar, pressione Ctrl+C. Visitas abertas persistem entre execuções; corrija manualmente dados de dias anteriores em uma versão futura.
- Avisar as pessoas sobre a captura, restringir acesso às fotos e definir retenção são requisitos do uso real.

## Configurar a câmera pelo painel (versão 2)

Abra **Configuração da câmera**, preencha IP, porta RTSP (normalmente 554), usuário, senha e o caminho do stream. Clique **Testar conexão**; se receber resolução de imagem, clique **Salvar e iniciar**. A foto atual com linha vertical aparecerá logo abaixo. Ajuste a posição e o sentido de entrada, salve novamente e teste uma passagem real em cada direção.

Os ajustes ficam em `data/camera.json` no computador local, inclusive a senha em texto simples. Restrinja acesso à pasta e não inclua `data/` ao compartilhar o projeto. Para atualizar a versão anterior, substitua os arquivos do projeto e mantenha sua pasta `data/` dentro de `camsecmon`. Se preferir, renomeie sua pasta antiga `permanencia-mvp` para `camsecmon`, sem apagar `data/`. Pare e reinicie o servidor após substituir esses arquivos.
