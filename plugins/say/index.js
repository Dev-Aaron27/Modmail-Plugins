
export default {
  apiVersion: 1,
  name: 'say',

  async start(api) {
    api.registerCommand('say', {
      level: 4,
      description: 'Send a message in the current channel.',

      async execute(context) {
        // Get the message supplied after .say
        const message = context.args?.join(' ').trim();

        if (!message) {
          return await context.respond(
            '❌ Please provide a message to send. Usage: `.say <message>`'
          );
        }

        // Find the channel where the command was used
        const channel = context.channel;

        if (!channel || typeof channel.send !== 'function') {
          return await context.respond(
            '❌ I could not find the channel to send the message in.'
          );
        }

        try {
          // Send the supplied message to that channel
          await channel.send({
            content: message,
            allowedMentions: {
              parse: []
            }
          });

          // Delete the command message if possible
          if (context.message?.deletable) {
            await context.message.delete().catch(() => {});
          }
        } catch (error) {
          api.log(`Say command error: ${error.message}`);

          await context.respond(
            '❌ I could not send that message. Check my channel permissions.'
          );
        }
      }
    });

    api.log('Say plugin loaded.');
  },

  async stop() {},

  hooks: {}
};
