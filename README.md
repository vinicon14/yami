# Yami Portable AI

Yami e uma assistente virtual portatil para rodar em um pendrive no Windows.

## Recursos

- Interface futurista local em `http://127.0.0.1:8765/`
- Uso por texto, microfone e resposta por voz
- Modelo local via Ollama ou API online
- Deteccao de modelo por chave API
- Anexos no prompt, incluindo texto, codigo, PDF simples, DOCX, PPTX, XLSX e imagens
- Acesso a internet por acoes internas `web_search` e `web_fetch`
- Acesso aos arquivos dentro da pasta da Yami
- Autoedicao segura dentro da propria pasta do projeto

## Como usar

1. Copie a pasta para o pendrive.
2. Execute `YamiPortable.exe`.
3. Abra a interface no navegador.
4. Em `Studio`, escolha modelo local ou API online.

As configuracoes e conversas ficam na pasta `data/`, que nao deve ser enviada ao GitHub porque pode conter chaves API e historico local.

## Desenvolvimento

Para recompilar o executavel:

```bat
build_exe.bat
```

O codigo principal fica em `yami_portable.py`.
