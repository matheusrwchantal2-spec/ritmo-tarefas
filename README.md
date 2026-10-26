# Ritmo — aplicativo local de tarefas

Aplicativo pessoal de tarefas em português. O servidor usa a biblioteca padrão do Python; a interface abre no navegador e os dados ficam em um banco SQLite local.

## Como abrir no Windows

1. Abra a pasta `tarefas-app` no Explorador de Arquivos.
2. Clique na barra de endereço da pasta, digite `powershell` e pressione Enter.
3. No terminal que abrir, execute:

   ```powershell
   py app.py
   ```

4. Abra `http://127.0.0.1:8765` no navegador.
5. Para encerrar o servidor, volte ao terminal e pressione `Ctrl+C`.

Mantenha o terminal aberto enquanto estiver usando o aplicativo. Para voltar a usar as tarefas depois, inicie o servidor novamente; elas continuam salvas.

## O que está incluído

- Criação, edição, conclusão, reabertura e exclusão de tarefas.
- Prazo opcional, com grupos para hoje, próximos sete dias e atrasadas.
- Pesquisa no título e na descrição, sem diferenciar maiúsculas/minúsculas ou acentos.
- Histórico por dia com horários para tarefas adicionadas, concluídas, reabertas e excluídas.
- Dados locais em `tasks.sqlite3`, criado automaticamente ao iniciar o servidor.

O backup/exportação não faz parte desta primeira versão.
