апуск FloTrader на RockyLinux (без sudo)                                                                                
                                      
  1. Встановлення Python через pyenv                                                                                          
  # Встановити pyenv                                                                                                       
  curl https://pyenv.run | bash                                                                                            

  # Додати в ~/.bashrc                                                                                                     
  echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
  echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
  echo 'eval "$(pyenv init -)"' >> ~/.bashrc

  # Перезавантажити shell                                                                                                  
  source ~/.bashrc

  # Встановити Python 3.11                                                                                                 
  pyenv install 3.11.7
  pyenv global 3.11.7

  # Перевірити                                                                                                             
  python --version

  2. Клонування та налаштування проекту

  # Клонувати репозиторій (або скопіювати файли)                                                                           
  cd ~
  mkdir -p trading
  cd trading
  # scp -r user@windows-pc:/d/Trading/newFlo .                                                                             
  # або git clone ...                                                                                                      

  cd newFlo

  # Створити віртуальне середовище                                                                                         
  python -m venv venv
  source venv/bin/activate

  # Встановити залежності                                                                                                  
  pip install --upgrade pip
  pip install -e .
  # або якщо немає pyproject.toml:                                                                                         
  pip install pybit websockets aiohttp pandas numpy pydantic pyyaml loguru aiosqlite python-telegram-bot click

  3. Конфігурація

  # Редагувати config.yaml                                                                                                 
  nano config.yaml

  Важливі налаштування:
  mode: live                                                                                                               

  telegram:                                                                                                                
    enabled: true                                                                                                          
    token: "YOUR_BOT_TOKEN"                                                                                                
    chat_id: "YOUR_CHAT_ID"                                                                                                

  bybit:                                                                                                                   
    testnet: false                                                                                                         
    api_key: "YOUR_API_KEY"                                                                                                
    api_secret: "YOUR_API_SECRET"                                                                                          

  4. Створення systemd user service (без sudo)

  # Створити директорію для user services                                                                                  
  mkdir -p ~/.config/systemd/user

  # Створити service файл                                                                                                  
  cat > ~/.config/systemd/user/flotrader.service << 'EOF'                                                                  
  [Unit]
  Description=FloTrader Live Trading Bot
  After=network.target

  [Service]
  Type=simple
  WorkingDirectory=%h/trading/newFlo
  ExecStart=%h/trading/newFlo/venv/bin/python -m app live --config config.yaml
  Restart=always
  RestartSec=10
  Environment=PYTHONUNBUFFERED=1

  [Install]
  WantedBy=default.target
  EOF

  # Перезавантажити systemd user daemon                                                                                    
  systemctl --user daemon-reload

  # Увімкнути автозапуск при логіні                                                                                        
  systemctl --user enable flotrader

  # Зберігати сервіси після виходу з сесії                                                                                 
  loginctl enable-linger $USER

  5. Керування сервісом

  # Запустити                                                                                                              
  systemctl --user start flotrader

  # Зупинити                                                                                                               
  systemctl --user stop flotrader

  # Перезапустити                                                                                                          
  systemctl --user restart flotrader

  # Статус                                                                                                                 
  systemctl --user status flotrader

  # Логи (live)                                                                                                            
  journalctl --user -u flotrader -f

  # Логи за останню годину                                                                                                 
  journalctl --user -u flotrader --since "1 hour ago"                                                                      

  6. Альтернатива: запуск через screen/tmux

  # Через screen                                                                                                           
  screen -S flotrader
  source ~/trading/newFlo/venv/bin/activate
  cd ~/trading/newFlo
  python -m app live --config config.yaml
  # Ctrl+A, D - відключитись                                                                                               
  # screen -r flotrader - підключитись назад                                                                               

  # Через tmux                                                                                                             
  tmux new -s flotrader
  source ~/trading/newFlo/venv/bin/activate
  cd ~/trading/newFlo
  python -m app live --config config.yaml
  # Ctrl+B, D - відключитись                                                                                               
  # tmux attach -t flotrader - підключитись назад                                                                          

  7. Корисні команди

  # Перевірити чи працює                                                                                                   
  ps aux | grep flotrader

  # Подивитись логи додатку                                                                                                
  tail -f ~/trading/newFlo/logs/flotrader_*.log                                                                            

  # Backtest                                                                                                               
  cd ~/trading/newFlo
  source venv/bin/activate
  python -m app backtest --config config.yaml

  # Перевірити trades                                                                                                      
  sqlite3 live_trades.db "SELECT * FROM trades ORDER BY exit_time DESC LIMIT 10;"                                          
  cat live_trades/trades_*.csv

  8. Копіювання даних з Windows

  # На Linux машині                                                                                                        
  scp -r user@windows-ip:/d/Trading/newFlo/live_data ~/trading/newFlo/

  # Або через rsync (швидше для великих файлів)                                                                            
  rsync -avz --progress user@windows-ip:/d/Trading/newFlo/live_data ~/trading/newFlo/

  Структура на сервері

  ~/trading/newFlo/
  ├── venv/                 # Python virtual environment
  ├── config.yaml           # Конфігурація
  ├── live_data/            # Записані дані з біржі
  ├── live_trades/          # CSV трейдів
  ├── live_trades.db        # SQLite база трейдів
  ├── logs/                 # Логи
  └── app/                  # Код