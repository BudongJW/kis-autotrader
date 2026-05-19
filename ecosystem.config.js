// PM2 ecosystem 설정 — Oracle Cloud VM에서 kis-autotrader 실행
//
// 사용법:
//   pm2 start ecosystem.config.js
//   pm2 save && pm2 startup
//
// 로그 확인:
//   pm2 logs kis-day       # 한국장 봇
//   pm2 logs kis-night     # 미국장 봇
//   pm2 logs kis-learn     # 학습 파이프라인

module.exports = {
  apps: [
    // ── 한국장 봇: 평일 08:55~15:30 KST ──
    {
      name: 'kis-day',
      script: '.venv/bin/python',
      args: '-m src.bot.single_run --loop',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      // 평일 08:55 시작, 15:35 자동 종료 (봇 내부 로직)
      cron_restart: '55 8 * * 1-5',
      autorestart: false,    // 봇이 장 마감 후 정상 종료하므로
      max_restarts: 3,
      restart_delay: 60000,  // 재시작 시 1분 대기
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-day-error.log',
      out_file: 'logs/pm2-day-out.log',
      merge_logs: true,
      max_memory_restart: '512M',
    },

    // ── 미국장 야간 봇: 평일 23:25~06:15 KST (동절기) ──
    {
      name: 'kis-night',
      script: '.venv/bin/python',
      args: '-m src.bot.night_run --loop',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      // 평일 23:25 시작 (일~목 밤 = 월~금 미국장)
      cron_restart: '25 23 * * 0-4',
      autorestart: false,
      max_restarts: 3,
      restart_delay: 60000,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-night-error.log',
      out_file: 'logs/pm2-night-out.log',
      merge_logs: true,
      max_memory_restart: '512M',
    },

    // ── 장 전 학습: 평일 08:30 KST ──
    {
      name: 'kis-learn-pre',
      script: '.venv/bin/python',
      args: '-m src.market_learner --phase pre',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      cron_restart: '30 8 * * 1-5',
      autorestart: false,
      max_restarts: 1,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-learn-error.log',
      out_file: 'logs/pm2-learn-out.log',
      merge_logs: true,
    },

    // ── 장 후 학습 (한국): 평일 16:00 KST ──
    {
      name: 'kis-learn-post',
      script: '.venv/bin/python',
      args: '-m src.market_learner --phase post',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      cron_restart: '0 16 * * 1-5',
      autorestart: false,
      max_restarts: 1,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-learn-error.log',
      out_file: 'logs/pm2-learn-out.log',
      merge_logs: true,
    },

    // ── 장 후 학습 (미국): 평일 06:30 KST ──
    {
      name: 'kis-learn-us',
      script: '.venv/bin/python',
      args: '-m src.market_learner --phase post_us',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      cron_restart: '30 6 * * 2-6',  // 화~토 아침 = 월~금 미국장 종료 후
      autorestart: false,
      max_restarts: 1,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-learn-error.log',
      out_file: 'logs/pm2-learn-out.log',
      merge_logs: true,
    },

    // ── 주간 최적화: 일요일 10:00 KST ──
    {
      name: 'kis-optimize',
      script: '.venv/bin/python',
      args: '-m src.optimizer',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      cron_restart: '0 10 * * 0',
      autorestart: false,
      max_restarts: 1,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-optimize-error.log',
      out_file: 'logs/pm2-optimize-out.log',
      merge_logs: true,
    },

    // ── 포트폴리오 저널 업데이트: 평일 15:35 + 06:15 KST ──
    {
      name: 'kis-journal',
      script: '.venv/bin/python',
      args: '-m src.journal_quick',
      cwd: '/home/ubuntu/kis-autotrader',
      env: {
        PYTHONPATH: '.',
        PYTHONIOENCODING: 'utf-8',
        TZ: 'Asia/Seoul',
      },
      cron_restart: '35 15 * * 1-5',
      autorestart: false,
      max_restarts: 1,
      log_date_format: 'YYYY-MM-DD HH:mm:ss',
      error_file: 'logs/pm2-journal-error.log',
      out_file: 'logs/pm2-journal-out.log',
      merge_logs: true,
    },
  ],
};
