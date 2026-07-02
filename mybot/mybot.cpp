#include <dpp/dpp.h>
#include <nlohmann/json.hpp>
#include <iostream>

const std::string BOT_TOKEN = "REDACTED";
using json = nlohmann::json;

int main() {
    // Instantiate your bot cluster using your Discord Token
    dpp::cluster bot(BOT_TOKEN);

    bot.on_log(dpp::utility::cout_logger());
    // Listen for slash commands
    bot.on_slashcommand([&bot](const dpp::slashcommand_t& event) {
        
        if (event.command.get_command_name() == "portfolio") {
            // Inform Discord we need an extra second to process this command
            event.thinking();

            // Perform an asynchronous HTTP GET request to our local Python server
            bot.request("http://127.0.0.1:8000/portfolio", dpp::m_get, [event](const dpp::http_request_completion_t& response) {
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
                        double crypto_equity = data["crypto_equity"];
                        double total_equity = data["total_equity"];

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
                        event.edit_response("Error from service: " + data["message"].get<std::string>());
                    }
                } 
                catch (const std::exception& e) {
                    event.edit_response("Error parsing portfolio metrics.");
                }
            });
        }
    });



    // Register slash commands to Discord on startup
    bot.on_ready([&bot](const dpp::ready_t& event) {
    if (dpp::run_once<struct register_bot_commands>()) {
        dpp::snowflake my_guild_id = 458099653267947521; 

        bot.guild_command_create(
            dpp::slashcommand("portfolio", "Check current Robinhood portfolio performance", bot.me.id),
            my_guild_id
        );
    }

    // Every 2 hours, send a bot message on how my portfolio is doing
       bot.start_timer([&bot](const dpp::timer& timer) { // this is the on_time function that is called after set amount of seconds
	            /* Create a timer when the bot starts. */
	            bot.request("http://127.0.0.1:8000/portfolio", dpp::m_get, [&bot, timer](const dpp::http_request_completion_t& callback) {
	                if (callback.status != 200) {
                     bot.message_create(dpp::message(458099654186369025,"Failed to contact the portfolio microservice."));
                     return;
                  }
                  try {
                    // Parse the raw response body using nlohmann/json 
                    auto data = json::parse(callback.body);

                    if (data["status"] == "success") {
                        double equity = data["equity"].get<double>();
                        double market_val = data["market_value"].get<double>();
                        double crypto_equity = data["crypto_equity"];
                        double total_equity = data["total_equity"];

                        // Build an attractive Discord embed with the data
                        dpp::embed embed = dpp::embed()
                            .set_color(dpp::colors::emerald_green)
                            .set_title("📈 Robinhood Portfolio Status")
                            .add_field("Stocks Equity", "$" + std::to_string(equity), true)
                            .add_field("Crypto Equity", "$" + std::to_string(crypto_equity), true)
                            .add_field("Total Equity", "$" + std::to_string(total_equity), true)
                            .add_field("Market Value", "$" + std::to_string(market_val), true)
                            .set_timestamp(time(0));

                        bot.message_create(dpp::message(458099654186369025,embed));
                    } else {
                        bot.message_create(dpp::message(458099654186369025,"Error with something"));
                    }
                } 
                catch (const std::exception& e) {
                    bot.message_create(dpp::message(458099654186369025,"Error parsing portfolio metrics."));
                }
	            });
	        }, 7200); /* Do it every 10 seconds. Timers also start with this delay. */
    });
    bot.start(dpp::st_wait);
    return 0;
}


