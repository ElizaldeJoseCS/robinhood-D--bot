#include <dpp/dpp.h>
#include <nlohmann/json.hpp>
#include <iostream>
#include <string>
#include <sstream>
#include <iomanip>
#include <ctime>
#include <cstdlib>
#include <functional>

using json = nlohmann::json;

/* std::to_string on a double always emits 6 decimals ("1234.560000"), which looks
   wrong in a money field. These trim it to something readable. */
static std::string money(double v) {
    std::ostringstream os;
    os << std::fixed << std::setprecision(2) << v;
    return os.str();
}

static std::string pct(double v) {
    std::ostringstream os;
    os << std::fixed << std::setprecision(1) << v << "%";
    return os.str();
}

/* Reports a failed edit instead of letting the interaction hang. Without this the
   default handler logs through the cluster logger and the user just sees the
   command think forever. */
static dpp::command_completion_event_t report_edit(std::string tag) {
    return [tag](const dpp::confirmation_callback_t& cc) {
        if (cc.is_error()) {
            std::cerr << "[" << tag << "] edit_response failed: code "
                      << cc.get_error().code << " " << cc.get_error().message << std::endl;
        }
    };
}

/* Defer, THEN do the work.

   thinking() is itself an async POST to Discord, and edit_response targets the
   response that POST creates. Endpoints answered from cache return in ~3ms, which
   is faster than the deferral round-trip, so calling edit_response straight after
   thinking() edits a response that does not exist yet — Discord rejects it and the
   command thinks forever. /portfolio hid this because its backend call takes ~0.9s.
   Running the work inside thinking()'s completion callback removes the race. */
static void defer_then(const dpp::slashcommand_t& event, std::function<void()> work) {
    event.thinking(false, [work](const dpp::confirmation_callback_t& defer) {
        if (defer.is_error()) {
            std::cerr << "[defer] thinking() failed: code " << defer.get_error().code
                      << " " << defer.get_error().message << std::endl;
            return;
        }
        work();
    });
}

int main() {
    const char* token = std::getenv("DISCORD_BOT_TOKEN");
    const char* guild_id_str = std::getenv("GUILD_ID");
    const char* channel_id_str = std::getenv("CHANNEL_ID");

    if (!token || !guild_id_str || !channel_id_str) {
        std::cerr << "Missing required environment variables. Set DISCORD_BOT_TOKEN, GUILD_ID, and CHANNEL_ID in your .env file.\n";
        return 1;
    }

    dpp::snowflake my_guild_id = std::stoull(guild_id_str);
    dpp::snowflake my_channel_id = std::stoull(channel_id_str);

    dpp::cluster bot(token);

    /* std::cout is block-buffered when stdout is not a TTY, so cout_logger's output
       never reached the journal and every D++ error was invisible. std::cerr is
       unit-buffered and std::endl flushes. */
    bot.on_log([](const dpp::log_t& log) {
        if (log.severity >= dpp::ll_info) {
            std::cerr << "[dpp] " << log.message << std::endl;
        }
    });
    
    // Listen for slash commands
    bot.on_slashcommand([&bot](const dpp::slashcommand_t& event) {
        
        if (event.command.get_command_name() == "portfolio") {
            defer_then(event, [&bot, event]() {
            bot.request("http://127.0.0.1:8000/portfolio", dpp::m_get, [&bot, event](const dpp::http_request_completion_t& response) {
                // Check if HTTP transfer was successful
                if (response.status != 200) {
                    event.edit_response("Failed to contact the portfolio microservice.");
                    return;
                }

                try {
                    // Parse the raw response body using nlohmann/json
                    auto data = json::parse(response.body);
                    if (data["status"] == "success") {
                        double equity = data["equity"].get<double>();
                        double market_val = data["market_value"].get<double>();
                        double crypto_equity = data["crypto_equity"].get<double>();
                        double total_equity = data["total_equity"].get<double>();

                        // Build an attractive Discord embed with the data
                        dpp::embed embed = dpp::embed()
                            .set_color(dpp::colors::emerald_green)
                            .set_title("📈 Robinhood Portfolio Status")
                            .add_field("Stocks Equity", "$" + std::to_string(equity), true)
                            .add_field("Crypto Equity", "$" + std::to_string(crypto_equity), true)
                            .add_field("Total Equity", "$" + std::to_string(total_equity), true)
                            .add_field("Market Value", "$" + std::to_string(market_val), true)
                            .set_timestamp(time(0));

                        event.edit_response(dpp::message(event.command.channel_id, embed));
                    } else {
                        event.edit_response("Error from service (portfolio): " + data["message"].get<std::string>());
                    }
                } 
                catch (const std::exception& e) {
                    event.edit_response("Error parsing portfolio metrics.");
                }
            });
            });
        } 
        
        if (event.command.get_command_name() == "risk") {
            defer_then(event, [&bot, event]() {
            bot.request("http://127.0.0.1:8000/risk", dpp::m_get, [&bot, event](const dpp::http_request_completion_t& response) {
                if (response.status != 200) {
                    event.edit_response("Failed to contact the portfolio microservice.");
                    return;
                }

                try {
                    auto data = json::parse(response.body);
                    if (data["status"] != "success") {
                        event.edit_response("Risk model unavailable: " +
                            data.value("message", std::string("still computing, try again shortly.")));
                        return;
                    }

                    /* Second hop for the rendered surface. The image is fetched inside this
                       callback so the embed and its attachment go out as one message — Discord
                       resolves attachment:// only against files in the same payload. */
                    bot.request("http://127.0.0.1:8000/risk/chart.png", dpp::m_get, [&bot, event, data](const dpp::http_request_completion_t& img) {
                      /* This runs on a later callback, so the outer try/catch cannot
                         reach it — an exception here would leave the reply unsent. */
                      try {
                        double total = data["total_value"].get<double>();
                        double var95 = data["var_95"].get<double>();

                        dpp::embed embed = dpp::embed()
                            .set_color(0x5865F2)
                            .set_title("Portfolio Risk — Monte Carlo")
                            .set_description(
                                std::to_string(data["paths"].get<int>()) + " correlated paths over " +
                                std::to_string(data["horizon_days"].get<int>()) + " trading days, drawn from the "
                                "Cholesky factor of the covariance matrix of " +
                                std::to_string(data["holdings"].get<int>()) + " holdings.")
                            .add_field("95% VaR", "$" + money(var95) + "  (" + pct(data["var_95_pct"].get<double>()) + ")", true)
                            .add_field("95% CVaR", "$" + money(data["cvar_95"].get<double>()), true)
                            .add_field("Annualised vol", pct(data["annual_vol_pct"].get<double>()), true)
                            .add_field("Median outcome", "$" + money(data["median_terminal"].get<double>()), true)
                            .add_field("5th / 95th pct", "$" + money(data["p05_terminal"].get<double>()) +
                                                         " / $" + money(data["p95_terminal"].get<double>()), true)
                            .add_field("P(loss)", pct(data["prob_loss_pct"].get<double>()), true)
                            .add_field("Median max drawdown", pct(data["median_max_drawdown_pct"].get<double>()), true)
                            .add_field("Start value", "$" + money(total), true)
                            .set_footer(dpp::embed_footer().set_text("Simulated from 1y of daily log returns"))
                            .set_timestamp(time(0));

                        dpp::message msg;
                        if (img.status == 200 && !img.body.empty()) {
                            msg.add_file("risk.png", img.body, "image/png");
                            embed.set_image("attachment://risk.png");
                        }
                        msg.add_embed(embed);
                        event.edit_response(msg, report_edit("risk"));
                      }
                      catch (const std::exception& e) {
                        std::cerr << "[risk] render failed: " << e.what() << std::endl;
                        event.edit_response("Error rendering the risk model.", report_edit("risk-fallback"));
                      }
                    });
                }
                catch (const std::exception& e) {
                    event.edit_response("Error parsing risk metrics.", report_edit("risk-parse"));
                }
            });
            });
        }

        if (event.command.get_command_name() == "recommend") {
            defer_then(event, [&bot, event]() {
            bot.request("http://127.0.0.1:8000/recommendations", dpp::m_get, [&bot, event](const dpp::http_request_completion_t& response) {
                if (response.status != 200) {
                    event.edit_response("Failed to contact portfolio microservice");
                    return;
                }

                try {
                    auto data = json::parse(response.body);
                    if (data["status"] == "processing") {
                         event.edit_response("The stock evaluation pipeline is still calculating market trends. Please try again in a few minutes!");
                         return;
                    }
                    
                    if (data["status"] == "success") {
                        // 1. Safely extract values into local strings
                        std::string d1 = (data["daily"].size() > 0) ? data["daily"][0].get<std::string>() : "None Found";
                        std::string d2 = (data["daily"].size() > 1) ? data["daily"][1].get<std::string>() : "None Found";

                        std::string w1 = (data["weekly"].size() > 0) ? data["weekly"][0].get<std::string>() : "None Found";
                        std::string w2 = (data["weekly"].size() > 1) ? data["weekly"][1].get<std::string>() : "None Found";

                        std::string m1 = (data["monthly"].size() > 0) ? data["monthly"][0].get<std::string>() : "None Found";

                        int64_t updated = data["last_updated"].get<int64_t>();
                        
                        // 2. Build the embed using the safe strings (Prevents out-of-bounds crashes!)
                        dpp::embed embed = dpp::embed()
                            .set_color(dpp::colors::red)
                            .set_title("Recommended Stocks")
                            .add_field("Daily Buy/Sell: ", d1 + ", " + d2, true)
                            .add_field("Weekly Buy/Sell: ", w1 + ", " + w2, true)
                            .add_field("Long Term: ", m1, true)
                            .add_field("Last updated: ", "<t:" + std::to_string(updated) + ":R>", true)
                            .set_timestamp(time(0));
                        
                        event.edit_response(dpp::message(event.command.channel_id, embed));
                    } else {
                        event.edit_response("Error from service: " + data["message"].get<std::string>());
                    }
                }
                catch (const std::exception& e) {
                    event.edit_response("Error parsing recommendations.");
                }
            });
            });
        }
    });

    // Register slash commands to Discord on startup
    bot.on_ready([&bot, my_guild_id, my_channel_id](const dpp::ready_t& event) {
        if (dpp::run_once<struct register_bot_commands>()) {
            dpp::slashcommand portfolio("portfolio", "Check current Robinhood portfolio performance", bot.me.id);
            dpp::slashcommand recommend("recommend", "Recommendations of stocks to buy", bot.me.id);
            dpp::slashcommand risk("risk", "Monte Carlo risk model of the live portfolio", bot.me.id);
            bot.guild_bulk_command_create({ portfolio, recommend, risk }, my_guild_id);
        }

        // Every 5 hours (18000 seconds), send a bot message status report.
        // Guarded by run_once: on_ready fires again on every gateway reconnect,
        // and without this each reconnect would stack another duplicate timer.
        if (dpp::run_once<struct start_portfolio_timer>()) {
            bot.start_timer([&bot, my_channel_id](const dpp::timer& timer) {
                bot.request("http://127.0.0.1:8000/portfolio", dpp::m_get, [&bot, my_channel_id](const dpp::http_request_completion_t& callback) {
                    if (callback.status != 200) {
                        bot.message_create(dpp::message(my_channel_id, "Failed to contact the portfolio microservice."));
                        return;
                    }
                    try {
                        auto data = json::parse(callback.body);

                        if (data["status"] == "success") {
                            double equity = data["equity"].get<double>();
                            double market_val = data["market_value"].get<double>();
                            double crypto_equity = data["crypto_equity"].get<double>();
                            double total_equity = data["total_equity"].get<double>();

                            dpp::embed embed = dpp::embed()
                                .set_color(dpp::colors::emerald_green)
                                .set_title("📈 Robinhood Portfolio Status")
                                .add_field("Stocks Equity", "$" + std::to_string(equity), true)
                                .add_field("Crypto Equity", "$" + std::to_string(crypto_equity), true)
                                .add_field("Total Equity", "$" + std::to_string(total_equity), true)
                                .add_field("Market Value", "$" + std::to_string(market_val), true)
                                .set_timestamp(time(0));

                            bot.message_create(dpp::message(my_channel_id, embed));
                        } else {
                            bot.message_create(dpp::message(my_channel_id, "Error updating portfolio tracker."));
                        }
                    } 
                    catch (const std::exception& e) {
                        bot.message_create(dpp::message(my_channel_id, "Error parsing portfolio loop callback metrics."));
                    }
                });
            }, 18000);
        }
    });

    /* Connection watchdog.

       The bot can sit alive with a dead gateway: the process never exits, so
       systemd's Restart=always never fires and it silently stops posting. This
       ran for 3+ days once. Registered here rather than inside on_ready because
       the worst case is a bot that never connects at all, where on_ready never
       fires and a handler registered there would never run.

       Signals checked: the shard exists, reports connected, has seen READY, and
       has ACKed a heartbeat recently (Discord's interval is ~41s). */
    const time_t process_start = time(nullptr);
    bot.start_timer([&bot, process_start](const dpp::timer&) {
        static time_t unhealthy_since = 0;
        const time_t now = time(nullptr);

        if (now - process_start < 180) {
            return; // startup grace — the first connection takes a few seconds
        }

        dpp::discord_client* shard = bot.get_shard(0);
        const bool healthy = shard != nullptr
                          && shard->is_connected()
                          && shard->ready
                          && (now - shard->last_heartbeat_ack) < 120;

        if (healthy) {
            if (unhealthy_since != 0) {
                std::cerr << "[watchdog] gateway recovered" << std::endl;
                unhealthy_since = 0;
            }
            return;
        }

        if (unhealthy_since == 0) {
            unhealthy_since = now;
            std::cerr << "[watchdog] gateway unhealthy, starting countdown" << std::endl;
            return;
        }

        if (now - unhealthy_since >= 300) {
            std::cerr << "[watchdog] gateway down " << (now - unhealthy_since)
                      << "s — exiting for systemd to restart" << std::endl;
            std::cerr.flush();
            std::_Exit(1); // hard exit: skip static destructors while D++ threads are live
        }
    }, 60);

    bot.start(dpp::st_wait);
    return 0;
}
