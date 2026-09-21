-- minimal config for the NESTED test compositor (never used by the real session); borders/rounding on to test decoration ordering
hl.monitor({ output = "", mode = "2560x1440@60", position = "auto", scale = 1 })
hl.config({ misc = { disable_hyprland_logo = true, disable_splash_rendering = true },
            general = { border_size = 6, gaps_in = 6, gaps_out = 20, col = { active_border = "rgba(ffcc00ff)", inactive_border = "rgba(ffcc00ff)" } },
            decoration = { rounding = 24 } })
