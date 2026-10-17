# Friends server (Supabase)

The schema that was pasted into the Supabase SQL editor lives in `schema.sql`.
Only needed once, for the whole friends network. People installing LazyTrack don't need to do anything here.

## Admin page (ideas box, announcements, users, test lab)

`admin.sql` sets up the ideas box (the + button in the app), announcements, and the numbers behind the
Admin page. Put your username on the line near the top, paste the whole file into the SQL editor, Run.
That account then sees **🛠 Admin** at the top of the app. Safe to run again (e.g. to add another admin).
The admin only sees counts and Mochi info, never anyone's habits; the rules live in the database.

## The egg and pokes

`egg.sql` lets the LazyTrack Egg log habits (they wait in `egg_inbox` until your LazyTrack picks them up
and deletes them) and lets friends poke each other's Mochi from the egg or the app (`pokes`, at most 10
a minute). Paste it into the SQL editor and Run, once. Safe to run again. Everything in it is private:
only you can see your inbox and the egg's habit list, and only friends can poke you.
