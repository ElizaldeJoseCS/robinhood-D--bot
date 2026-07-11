#include <dpp/dpp.h>
#include <nlohmann/json.hpp>
#include <iostream>
#include <string> 

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
                    std::cout << "Parsing rn: \n";
                    if (data["status"] == "success") {
                        std::cout << "SUCCESS\n";
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
        if(event.command.get_command_name() == "recommend"){

          event.thinking();
          bot.request("http://127.0.0.1:8000/recommendations", dpp::m_get, [event](const dpp::http_request_completion_t response){
              if(response.status != 200)
              {
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
                        // Safely extract daily picks
                        std::string daily_1 = (data["daily"].size() > 0) ? data["daily"][0].get<std::string>() : "None Found";
                        std::string daily_2 = (data["daily"].size() > 1) ? data["daily"][1].get<std::string>() : "None Found";

                        // Safely extract weekly picks (Fixes the crash!)
                        std::string weekly_1 = (data["weekly"].size() > 0) ? data["weekly"][0].get<std::string>() : "None Found";
                        std::string weekly_2 = (data["weekly"].size() > 1) ? data["weekly"][1].get<std::string>() : "None Found";

                        // Safely extract monthly picks
                        std::string monthly = (data["monthly"].size() > 0) ? data["monthly"][0].get<std::string>() : "None Found";

                        int64_t updated = data["last_updated"].get<int64_t>();
                        
                       dpp::embed embed = dpp::embed()
                          .set_color(dpp::colors::red)
                          .set_title("Recommended Stocks")
                          .add_field("Daily Buy/Sell: ", daily_1 + ", " + daily_2, true)
                          .add_field("Weekly Buy/Sell: ", weekly_1 + ", " + weekly_2, true)
                          .add_field("Long Term: ", monthly, true)
                          .add_field("Last updated: ", "<t:" + std::to_string(updated) + ":R>", true)
                          .set_timestamp(time(0));
                       event.edit_response(dpp::message(event.command.channel_id, embed));
                  }else{
                    event.edit_response("Error from service: " + data["message"].get<std::string>());
                  }
                }
              catch (const std::exception& e) {
                event.edit_response("Error parsing recommendations");
              }
          });
        }
    });



    // Register slash commands to Discord on startup
    bot.on_ready([&bot](const dpp::ready_t& event) {
    if (dpp::run_once<struct register_bot_commands>()) {
          dpp::slashcommand portfolio("portfolio", "Check current Robinhood portfolio performance", bot.me.id);
          dpp::slashcommand recommend ("recommend", "recommendations of stocks to buy", bot.me.id);
          dpp::snowflake my_guild_id = 458099653267947521;
          bot.guild_bulk_command_create({ portfolio, recommend}, my_guild_id);
    
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


