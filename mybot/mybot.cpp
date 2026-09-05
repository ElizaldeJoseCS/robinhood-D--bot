#include <dpp/dpp.h>
#include <nlohmann/json.hpp>
#include <iostream>
#include <string>
#include <cstdlib>

using json = nlohmann::json;

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

    bot.on_log(dpp::utility::cout_logger());
    
    // Listen for slash commands
    bot.on_slashcommand([&bot](const dpp::slashcommand_t& event) {
        
        if (event.command.get_command_name() == "portfolio") {
            // Inform Discord we need an extra second to process this command
            event.thinking();

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
        } 
        
        if (event.command.get_command_name() == "recommend") {
            event.thinking();
            
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
        }
    });

    // Register slash commands to Discord on startup
    bot.on_ready([&bot, my_guild_id, my_channel_id](const dpp::ready_t& event) {
        if (dpp::run_once<struct register_bot_commands>()) {
            dpp::slashcommand portfolio("portfolio", "Check current Robinhood portfolio performance", bot.me.id);
            dpp::slashcommand recommend("recommend", "Recommendations of stocks to buy", bot.me.id);
            bot.guild_bulk_command_create({ portfolio, recommend }, my_guild_id);
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

    bot.start(dpp::st_wait);
    return 0;
}
