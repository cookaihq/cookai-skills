// Called only by Git after the caller matched the exact HTTPS repository.
const prompt = process.argv.slice(2).join(' ');
if (/username/i.test(prompt)) process.stdout.write('x-access-token\n');
else if (/password/i.test(prompt) && process.env.GIT_SETUP_ASKPASS_TOKEN) {
  process.stdout.write(`${process.env.GIT_SETUP_ASKPASS_TOKEN}\n`);
} else process.exitCode = 1;
